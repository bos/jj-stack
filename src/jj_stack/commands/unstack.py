"""Close or locally forget the selected stack.

Passing `--cleanup` also removes `jj-stack`'s own review branches, forgets any local bookmarks
that still point at those branches, and clears saved tracking data for the selected stack.

If you asked `jj-stack` to use your own bookmarks with `submit --use-bookmarks`, those are
preserved unless `cleanup_user_bookmarks = true`. Use `--pull-request` to close by PR number or
URL.

Use `unstack --cleanup --pull-request <pr>` to retire an orphaned PR shown by `list`.
Use `unstack --local` to forget local review tracking without closing PRs or deleting
bookmarks.

To preview the unstack plan without changing anything, use `--dry-run`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.commands._close_actions import (
    CloseAction,
    CloseActionBody,
    apply_bookmark_cleanup,
    emit_close_actions,
    find_managed_comments as _find_managed_comments,
    plan_bookmark_cleanup,
    retire_cached_change as _retire_cached_change,
)
from jj_stack.commands.close_orphan import (
    run_orphan_close,
    run_untracked_cleanup_pull_request,
    state_has_pull_request_record,
)
from jj_stack.errors import ErrorMessage, UsageError
from jj_stack.github.client import GithubClient, build_github_client
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.resolution import (
    GithubRepoAddress,
    resolve_github_target,
)
from jj_stack.github.stack_comments import stack_comment_label
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.review.bookmarks import (
    bookmark_ownership_for_source,
)
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_stack.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_orphaned_pull_request,
    resolve_pull_request_number,
    resolve_selected_revset,
)
from jj_stack.review.status import (
    PreparedRevision,
    PreparedStack,
    PreparedStatus,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    stream_status,
)
from jj_stack.state.journal import OperationJournal
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Stop reviewing a jj stack on GitHub"


@dataclass(frozen=True, slots=True)
class CloseResult:
    """Rendered close result for the selected repository."""

    actions: tuple[CloseAction, ...]
    applied: bool
    blocked: bool
    cleanup: bool
    github_error: ErrorMessage | None
    github_repository: GithubRepoAddress | None
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    selected_revset: str


@dataclass(frozen=True, slots=True)
class PreparedClose:
    """Locally prepared close inputs before any GitHub mutation."""

    cleanup: bool
    context: CommandContext
    dry_run: bool
    prepared_status: PreparedStatus


@dataclass(frozen=True, slots=True)
class LocalUnstackAction:
    """One local tracking record forgotten by `unstack --local`."""

    bookmark: str | None
    change_id: str
    subject: str


@dataclass(frozen=True, slots=True)
class LocalUnstackResult:
    """Rendered result for a local-only unstack."""

    actions: tuple[LocalUnstackAction, ...]
    dry_run: bool


@dataclass(frozen=True, slots=True)
class _CloseMutationRun:
    """Shared dependencies for close mutations and cleanup on inspected revisions."""

    commit_ids_by_change_id: dict[str, str]
    current_state: ReviewState
    github_client: GithubClient
    next_changes: dict[str, CachedChange]
    prepared_close: PreparedClose
    record_action: Callable[[CloseAction], None]
    journal: OperationJournal = OperationJournal.disabled()

    @property
    def bookmark_prefix(self) -> str:
        return self.prepared_close.context.config.bookmark_prefix

    @property
    def cleanup_user_bookmarks(self) -> bool:
        return self.prepared_close.context.config.cleanup_user_bookmarks

    @property
    def dry_run(self) -> bool:
        return self.prepared_close.dry_run

    @property
    def jj_client(self) -> JjClient:
        return self.prepared_close.prepared_status.prepared.client

    @property
    def remote_name(self) -> str | None:
        remote = self.prepared_close.prepared_status.prepared.remote
        return remote.name if remote is not None else None


@dataclass(frozen=True, slots=True)
class _CloseSelectedStack:
    """Normal close target selected by revset."""

    revset: str | None


@dataclass(frozen=True, slots=True)
class _CloseOrphanPullRequestTarget:
    """Orphaned saved PR record selected for explicit cleanup."""

    change_id: str
    pull_request_number: int
    state: ReviewState


@dataclass(frozen=True, slots=True)
class _CloseUntrackedPullRequestTarget:
    """Untracked PR selected for explicit cleanup."""

    pull_request_number: int
    state: ReviewState


type _CloseTarget = (
    _CloseSelectedStack | _CloseOrphanPullRequestTarget | _CloseUntrackedPullRequestTarget
)


def unstack(
    *,
    cleanup: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    local: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `unstack`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    if local and cleanup:
        raise UsageError("unstack --local cannot be combined with --cleanup.")
    command = "unstack --local" if local else ("unstack --cleanup" if cleanup else "unstack")
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command=command,
    ):
        if local:
            result = _run_local_unstack(
                context=context,
                dry_run=dry_run,
                pull_request=pull_request,
                revset=revset,
            )
            _print_local_unstack_result(result)
            return 0
        return _run_close(
            context=context,
            cleanup=cleanup,
            dry_run=dry_run,
            pull_request=pull_request,
            revset=revset,
        )


def _run_local_unstack(
    *,
    context: CommandContext,
    dry_run: bool,
    pull_request: str | None,
    revset: str | None,
) -> LocalUnstackResult:
    selected_revset = _resolve_local_unstack_revset(
        context=context,
        dry_run=dry_run,
        pull_request=pull_request,
        revset=revset,
    )
    with console.spinner(description="Inspecting jj stack"):
        stack = context.jj_client.discover_review_stack(
            selected_revset,
            allow_divergent=True,
            allow_immutable=True,
        )
    state = context.state_store.load()
    next_changes = dict(state.changes)
    actions: list[LocalUnstackAction] = []
    for revision in stack.revisions:
        cached_change = next_changes.pop(revision.change_id, None)
        if cached_change is None:
            continue
        actions.append(
            LocalUnstackAction(
                bookmark=cached_change.bookmark,
                change_id=revision.change_id,
                subject=revision.subject,
            )
        )
    if actions and not dry_run:
        context.state_store.save(state.model_copy(update={"changes": next_changes}))
    return LocalUnstackResult(actions=tuple(actions), dry_run=dry_run)


def _resolve_local_unstack_revset(
    *,
    context: CommandContext,
    dry_run: bool,
    pull_request: str | None,
    revset: str | None,
) -> str | None:
    if pull_request is not None:
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="unstack --local",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}")
        return resolved_revset

    command_label = "unstack --local --dry-run" if dry_run else "unstack --local"
    return resolve_selected_revset(
        command_label=command_label,
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )


def _print_local_unstack_result(result: LocalUnstackResult) -> None:
    if not result.actions:
        console.output("No local review tracking was found for the selected stack.")
        return
    if result.dry_run:
        console.output("Planned local unstack actions:")
    else:
        console.output("Applied local unstack actions:")
    icon = "~" if result.dry_run else "✓"
    for action in result.actions:
        revision_label = t"{action.subject} ({ui.change_id(action.change_id)})"
        if action.bookmark is not None:
            console.output(
                t"  {icon} tracking: forget local review tracking for {revision_label}, "
                t"preserving {ui.bookmark(action.bookmark)}"
            )
        else:
            console.output(
                t"  {icon} tracking: forget local review tracking for {revision_label}"
            )


def _run_close(
    *,
    cleanup: bool,
    context: CommandContext,
    dry_run: bool,
    pull_request: str | None,
    revset: str | None,
) -> int:
    target = _resolve_close_target(
        cleanup=cleanup,
        context=context,
        dry_run=dry_run,
        pull_request=pull_request,
        revset=revset,
    )
    if isinstance(target, _CloseOrphanPullRequestTarget):
        return asyncio.run(
            run_orphan_close(
                change_id=target.change_id,
                context=context,
                dry_run=dry_run,
                pull_request_number=target.pull_request_number,
                state=target.state,
            )
        )
    if isinstance(target, _CloseUntrackedPullRequestTarget):
        return asyncio.run(
            run_untracked_cleanup_pull_request(
                context=context,
                dry_run=dry_run,
                pull_request_number=target.pull_request_number,
                state=target.state,
            )
        )

    with console.spinner(description="Inspecting jj stack"):
        prepared_close = prepare_close(
            cleanup=cleanup,
            context=context,
            dry_run=dry_run,
            revset=target.revset,
        )
    result = stream_close(prepared_close=prepared_close)
    print_close_result(result)
    return 1 if result.blocked else 0


def _resolve_close_target(
    *,
    cleanup: bool,
    context: CommandContext,
    dry_run: bool,
    pull_request: str | None,
    revset: str | None,
) -> _CloseTarget:
    if pull_request is not None:
        if cleanup and revset is None:
            if not dry_run:
                context.state_store.require_writable()
            state = context.state_store.load()
            pull_request_number = resolve_pull_request_number(
                jj_client=context.jj_client,
                pull_request_reference=pull_request,
            )
            orphan_target = resolve_orphaned_pull_request(
                jj_client=context.jj_client,
                pull_request_reference=pull_request,
                state=state,
            )
            if orphan_target is not None:
                pull_request_number, change_id = orphan_target
                return _CloseOrphanPullRequestTarget(
                    change_id=change_id,
                    pull_request_number=pull_request_number,
                    state=state,
                )
            if not state_has_pull_request_record(
                pull_request_number=pull_request_number,
                state=state,
            ):
                return _CloseUntrackedPullRequestTarget(
                    pull_request_number=pull_request_number,
                    state=state,
                )
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="unstack",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}")
        return _CloseSelectedStack(revset=resolved_revset)

    command_label = "unstack"
    if cleanup and dry_run:
        command_label = "unstack --cleanup --dry-run"
    elif cleanup:
        command_label = "unstack --cleanup"
    elif dry_run:
        command_label = "unstack --dry-run"

    return _CloseSelectedStack(
        revset=resolve_selected_revset(
            command_label=command_label,
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        )
    )


def print_close_result(result: CloseResult) -> None:
    for message in remote_and_github_unavailable_messages(
        github_error=result.github_error,
        github_repository=result.github_repository,
        remote=result.remote,
        remote_error=result.remote_error,
    ):
        console.warning(message)
    if result.actions:
        emit_close_actions(
            actions=result.actions,
            applied=result.applied,
            blocked=result.blocked,
        )
    else:
        if result.applied:
            console.note("No close actions were needed for the selected stack.")
        else:
            console.output("Nothing to close on the selected stack.")


def prepare_close(
    *,
    cleanup: bool,
    context: CommandContext,
    dry_run: bool,
    revset: str | None,
) -> PreparedClose:
    """Resolve local close inputs before any GitHub inspection."""

    state_store = context.state_store
    if not dry_run:
        state_store.require_writable()
    fast_path = _prepare_untracked_close_fast_path(
        context=context,
        revset=revset,
    )
    if fast_path is not None:
        return PreparedClose(
            cleanup=cleanup,
            context=context,
            dry_run=dry_run,
            prepared_status=fast_path,
        )
    return PreparedClose(
        cleanup=cleanup,
        context=context,
        dry_run=dry_run,
        prepared_status=prepare_status(
            context=context,
            fetch_remote_state=cleanup,
            fetch_only_when_tracked=True,
            persist_bookmarks=False,
            revset=revset,
        ),
    )


def _prepare_untracked_close_fast_path(
    *,
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus | None:
    """Build the no-op close path without bookmark discovery.

    Both plain `unstack` and `unstack --cleanup` are true no-ops when the selected
    stack has no saved review identity at all. In that case we can skip
    bookmark-state discovery and GitHub preparation while still preserving the
    normal remote diagnostics and stale-operation retirement behavior.
    """

    client = context.jj_client
    state_store = context.state_store
    stack = client.discover_review_stack(
        revset,
        allow_divergent=True,
        allow_immutable=True,
    )
    state = state_store.load()

    status_revisions: list[PreparedRevision] = []
    for revision in stack.revisions:
        cached_change = state.changes.get(revision.change_id)
        if classify_saved_review_change(
            cached_change,
            local="present",
        ).saved_review_identity:
            return None
        status_revisions.append(
            PreparedRevision(
                bookmark=(cached_change.bookmark or "") if cached_change is not None else "",
                bookmark_source="generated",
                cached_change=cached_change,
                revision=revision,
            )
        )

    github_target = resolve_github_target(client.list_git_remotes())

    prepared = PreparedStack(
        bookmark_states={},
        bookmark_result_changed=False,
        client=client,
        remote=github_target.remote,
        remote_error=github_target.remote_error,
        stack=stack,
        state=state,
        state_changes=dict(state.changes),
        state_store=state_store,
        status_revisions=tuple(status_revisions),
    )
    return PreparedStatus(
        github_target=github_target,
        prepared=prepared,
        selected_revset=stack.selected_revset,
        base_parent_subject=stack.base_parent.subject,
    )


def stream_close(
    *,
    prepared_close: PreparedClose,
    on_action: Callable[[CloseAction], None] | None = None,
) -> CloseResult:
    """Inspect GitHub state for prepared close inputs and optionally stream actions."""

    prepared_status = prepared_close.prepared_status
    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            inspect_stack_comments=True,
            persist_cache_updates=False,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    return asyncio.run(
        _stream_close_async(
            on_action=on_action,
            prepared_close=prepared_close,
            status_result=status_result,
        )
    )


async def _stream_close_async(
    *,
    on_action: Callable[[CloseAction], None] | None,
    prepared_close: PreparedClose,
    status_result: StatusResult,
) -> CloseResult:
    prepared_status = prepared_close.prepared_status
    prepared = prepared_status.prepared
    github_repository = prepared_status.github_repository

    recorder = ActionRecorder[CloseAction](
        on_action=on_action,
        blocks=lambda action: action.status == "blocked",
    )

    if not status_result.revisions:
        return _close_result(
            actions=(),
            blocked=False,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )

    no_work = _inspected_close_has_no_work(revisions=status_result.revisions)

    if not no_work and (status_result.github_error is not None or github_repository is None):
        recorder.record(
            CloseAction(
                kind="close",
                body=(
                    "cannot close pull requests tracked by jj-stack without live "
                    "GitHub state; "
                    "fix GitHub access and retry"
                ),
                status="blocked",
            )
        )
        return _close_result(
            actions=recorder.as_tuple(),
            blocked=True,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )

    current_state = prepared.state_store.load() if not prepared_close.dry_run else prepared.state
    completed = False
    close_journal = OperationJournal.disabled()
    try:
        close_journal = _start_close_operation_log(
            prepared_close=prepared_close,
        )

        if no_work:
            completed = True
            return _close_result(
                actions=(),
                applied=False,
                blocked=False,
                github_error=status_result.github_error,
                github_repository=github_repository,
                prepared_close=prepared_close,
            )

        assert github_repository is not None
        blocked = False
        async with build_github_client(repository=github_repository) as github_client:
            run = _CloseMutationRun(
                commit_ids_by_change_id={
                    prepared_revision.revision.change_id: prepared_revision.revision.commit_id
                    for prepared_revision in prepared.status_revisions
                },
                current_state=current_state,
                github_client=github_client,
                journal=close_journal,
                next_changes=dict(current_state.changes),
                prepared_close=prepared_close,
                record_action=recorder.record,
            )
            progress_total = len(status_result.revisions) if on_action is None else 0
            with console.progress(
                description="Processing close actions",
                total=progress_total,
            ) as progress:
                # Process each revision in order, stopping on the first fail-closed block.
                for revision in status_result.revisions:
                    should_stop = await _process_close_revision(
                        change_status=classify_review_status_revision(revision),
                        revision=revision,
                        run=run,
                    )
                    progress.advance()
                    if should_stop:
                        blocked = True
                        break

        _save_close_progress(run=run)
        completed = True
        return _close_result(
            actions=recorder.as_tuple(),
            blocked=blocked or recorder.blocked,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )
    finally:
        if completed:
            completed_change_ids = tuple(
                prepared_revision.revision.change_id
                for prepared_revision in prepared.status_revisions
            )
            close_journal.append(
                "completed",
                {"ordered_change_ids": completed_change_ids},
            )


def _inspected_close_has_no_work(*, revisions: tuple[ReviewStatusRevision, ...]) -> bool:
    """Whether close has nothing to do for the inspected revisions.

    Both plain close and cleanup only act on changes jj-stack tracks: closing
    a linked pull request, forgetting a bookmark we saved, deleting a remote
    branch we pushed. None of those exist for a change without review
    identity, so either variant is a true no-op on such a stack. A
    config-pinned bookmark without review identity is intentionally ignored --
    we never pushed that branch and must not delete it.
    """

    for revision in revisions:
        cached = revision.cached_change
        if classify_saved_review_change(cached, local="present").saved_review_identity:
            return False
    return True


def _save_close_progress(*, run: _CloseMutationRun) -> None:
    """Persist saved close state when a live run changed tracked metadata."""

    if run.dry_run or run.next_changes == run.current_state.changes:
        return
    run.journal.record_saved_state_updates(
        before=run.current_state.changes,
        after=run.next_changes,
    )
    run.prepared_close.prepared_status.prepared.state_store.save(
        run.current_state.model_copy(update={"changes": run.next_changes})
    )


def _start_close_operation_log(
    *,
    prepared_close: PreparedClose,
) -> OperationJournal:
    """Write close operation log metadata for live runs."""

    if prepared_close.dry_run:
        return OperationJournal.disabled()

    prepared_status = prepared_close.prepared_status
    state_dir = prepared_status.prepared.state_store.require_writable()
    ordered_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    ordered_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    journal = OperationJournal.begin(
        state_dir,
        operation="unstack",
        options={"cleanup": prepared_close.cleanup},
        resolved_scope={
            "ordered_change_ids": ordered_change_ids,
            "ordered_commit_ids": ordered_commit_ids,
            "selected_revset": prepared_status.selected_revset,
        },
    )
    return journal


def _close_result(
    *,
    actions: tuple[CloseAction, ...],
    applied: bool | None = None,
    blocked: bool,
    github_error: ErrorMessage | None,
    github_repository: GithubRepoAddress | None,
    prepared_close: PreparedClose,
) -> CloseResult:
    prepared = prepared_close.prepared_status.prepared
    return CloseResult(
        actions=actions,
        applied=(not prepared_close.dry_run) if applied is None else applied,
        blocked=blocked,
        cleanup=prepared_close.cleanup,
        github_error=github_error,
        github_repository=github_repository,
        remote=prepared.remote,
        remote_error=prepared.remote_error,
        selected_revset=prepared_close.prepared_status.selected_revset,
    )


async def _process_close_revision(
    *,
    change_status: ReviewChangeStatus,
    revision: ReviewStatusRevision,
    run: _CloseMutationRun,
) -> bool:
    """Close one revision's PR, retire its tracking, and clean up when requested.

    Returns True when the revision fails closed and processing must stop.
    """

    lookup = revision.pull_request_lookup
    if lookup is None and not change_status.has_pull_request_lookup_failure:
        return False
    if change_status.pr_lifecycle == "ambiguous" or change_status.has_pull_request_lookup_failure:
        body = (
            lookup.message
            if lookup is not None and lookup.message is not None
            else "cannot safely determine the pull request for this path"
        )
        run.record_action(
            CloseAction(
                kind="close",
                body=body,
                status="blocked",
            )
        )
        return True

    cached_change = revision.cached_change or run.current_state.changes.get(revision.change_id)
    revision_label = t"{revision.subject} ({ui.change_id(revision.change_id)})"

    if change_status.pr_lifecycle == "missing":
        if cached_change is not None and cached_change.pr_state == "open":
            run.record_action(
                CloseAction(
                    kind="close",
                    body=(
                        t"cannot close {revision_label} because GitHub no longer reports a "
                        t"pull request for its branch; run {ui.cmd('view --fetch')} or "
                        t"{ui.cmd('relink')} before retrying"
                    ),
                    status="blocked",
                )
            )
            return True
        if (
            not run.prepared_close.cleanup
            or cached_change is None
            or not classify_saved_review_change(
                cached_change,
                local="present",
            ).saved_review_identity
        ):
            return False
        pr_state = cached_change.pr_state or "closed"
    else:
        if lookup is None:
            return False
        if cached_change is None:
            if lookup.pull_request is None:
                return False
            cached_change = CachedChange(
                bookmark=revision.bookmark,
                bookmark_ownership=bookmark_ownership_for_source(revision.bookmark_source),
                pr_number=lookup.pull_request.number,
                pr_state=lookup.pull_request.state,
                pr_url=lookup.pull_request.html_url,
                navigation_comment_id=(
                    revision.managed_comments_lookup.navigation_comment.id
                    if revision.managed_comments_lookup is not None
                    and revision.managed_comments_lookup.state == "resolved"
                    and revision.managed_comments_lookup.navigation_comment is not None
                    else None
                ),
                overview_comment_id=(
                    revision.managed_comments_lookup.overview_comment.id
                    if revision.managed_comments_lookup is not None
                    and revision.managed_comments_lookup.state == "resolved"
                    and revision.managed_comments_lookup.overview_comment is not None
                    else None
                ),
            )
        if change_status.pr_lifecycle == "open" and lookup.pull_request is not None:
            pull_request_number = lookup.pull_request.number
            run.record_action(
                CloseAction(
                    kind="pull request",
                    body=t"close PR #{pull_request_number} for {revision_label}",
                    status="planned" if run.dry_run else "applied",
                )
            )
            if not run.dry_run:
                with run.journal.mutation(
                    "close_pull_request",
                    change_id=revision.change_id,
                    pull_request_number=pull_request_number,
                ):
                    await run.github_client.close_pull_request(
                        pull_number=pull_request_number,
                    )
            pr_state = "closed"
        elif change_status.pr_lifecycle in {"closed", "merged"}:
            github_merged = (
                lookup.pull_request is not None and change_status.pr_lifecycle == "merged"
            )
            pr_state = (
                "merged" if github_merged or cached_change.pr_state == "merged" else "closed"
            )
        else:
            return False

    updated_change = _record_retired_cached_change(
        cached_change=cached_change,
        pr_state=pr_state,
        revision=revision,
        revision_label=revision_label,
        run=run,
    )
    if not run.prepared_close.cleanup:
        return False
    bookmark_states = run.prepared_close.prepared_status.prepared.bookmark_states
    await _cleanup_revision(
        bookmark_state=bookmark_states.get(
            revision.bookmark,
            BookmarkState(name=revision.bookmark),
        ),
        cached_change=updated_change,
        commit_id=run.commit_ids_by_change_id.get(revision.change_id),
        revision=revision,
        run=run,
    )
    return False


def _record_retired_cached_change(
    *,
    cached_change: CachedChange,
    pr_state: str,
    revision: ReviewStatusRevision,
    revision_label: CloseActionBody,
    run: _CloseMutationRun,
) -> CachedChange:
    updated_change = _retire_cached_change(cached_change, pr_state=pr_state)
    if updated_change != cached_change:
        run.next_changes[revision.change_id] = updated_change
        run.record_action(
            CloseAction(
                kind="tracking",
                body=t"stop review tracking for {revision_label}",
                status="planned" if run.dry_run else "applied",
            )
        )
    return updated_change


async def _cleanup_revision(
    *,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    commit_id: str | None,
    revision: ReviewStatusRevision,
    run: _CloseMutationRun,
) -> None:
    bookmark = cached_change.bookmark
    if bookmark is not None:
        cleanup_plan = plan_bookmark_cleanup(
            bookmark=bookmark,
            bookmark_state=bookmark_state,
            cached_change=cached_change,
            cleanup_user_bookmarks=run.cleanup_user_bookmarks,
            commit_id=commit_id,
            prefix=run.bookmark_prefix,
            record_action=run.record_action,
            remote_name=run.remote_name,
        )
        apply_bookmark_cleanup(
            bookmark=bookmark,
            cleanup_plan=cleanup_plan,
            commit_id=commit_id,
            journal=run.journal,
            record_action=run.record_action,
            remote_name=run.remote_name,
            run=run,
        )

    if cached_change.pr_number is None:
        return

    lookups = await _find_managed_comments(
        cached_navigation_comment_id=cached_change.navigation_comment_id,
        cached_overview_comment_id=cached_change.overview_comment_id,
        github_client=run.github_client,
        pull_request_number=cached_change.pr_number,
    )
    cleared_comment = False
    for lookup in lookups:
        if lookup.blocked_reason is not None:
            run.record_action(
                CloseAction(
                    kind=stack_comment_label(lookup.kind),
                    body=lookup.blocked_reason,
                    status="blocked",
                )
            )
            return
        if lookup.comment is None:
            continue
        cleared_comment = True
        run.record_action(
            CloseAction(
                kind=stack_comment_label(lookup.kind),
                body=(
                    f"delete {stack_comment_label(lookup.kind)} #{lookup.comment.id} from PR "
                    f"#{cached_change.pr_number}"
                ),
                status="planned" if run.dry_run else "applied",
            )
        )
        if not run.dry_run:
            with run.journal.mutation(
                "delete_issue_comment",
                change_id=revision.change_id,
                comment_id=lookup.comment.id,
                kind=lookup.kind,
                pull_request_number=cached_change.pr_number,
            ):
                await run.github_client.delete_issue_comment(
                    comment_id=lookup.comment.id,
                )

    if (
        cached_change.navigation_comment_id is not None
        or cached_change.overview_comment_id is not None
        or cleared_comment
    ):
        run.next_changes[revision.change_id] = cached_change.with_cleared_comments()
