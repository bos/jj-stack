"""Close the GitHub pull requests for the selected stack.

Passing `--cleanup` also removes `jj-review`'s own review branches, forgets any local bookmarks
that still point at those branches, and clears saved tracking data for the selected stack.

If you asked `jj-review` to use your own bookmarks with `submit --use-bookmarks`, those are
preserved unless `cleanup_user_bookmarks = true`. Use `--pull-request` to close by PR number or
URL.

Use `close --cleanup --pull-request <pr>` to retire an orphaned PR shown by `list`.

To preview the close plan without changing anything, use `--dry-run`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.commands._close_actions import (
    BookmarkCleanupPlan as _BookmarkCleanupPlan,
    CloseAction,
    CloseActionBody,
    apply_bookmark_cleanup,
    close_action_presentation as _close_action_presentation,
    find_managed_comment as _find_managed_comment,
    plan_bookmark_cleanup,
    render_close_action_message as _render_close_action_message,
    retire_cached_change as _retire_cached_change,
)
from jj_review.commands.close_orphan import (
    run_orphan_close,
    run_untracked_cleanup_pull_request,
    state_has_pull_request_record,
)
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.github.client import GithubClient, build_github_client
from jj_review.github.error_messages import (
    github_unavailable_message,
    remote_unavailable_message,
)
from jj_review.github.resolution import ParsedGithubRepo, parse_github_repo, select_submit_remote
from jj_review.github.stack_comments import stack_comment_label
from jj_review.jj import JjCliArgs, JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.bookmarks import (
    bookmark_ownership_for_source,
)
from jj_review.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_orphaned_pull_request,
    resolve_pull_request_number,
    resolve_selected_revset,
)
from jj_review.review.status import (
    PreparedRevision,
    PreparedStack,
    PreparedStatus,
    ReviewStatusRevision,
    prepare_status,
    stream_status,
)
from jj_review.state.journal import OperationJournal
from jj_review.state.operation_lock import acquire_operation_lock

HELP = "Stop reviewing a jj stack on GitHub"


@dataclass(frozen=True, slots=True)
class CloseResult:
    """Rendered close result for the selected repository."""

    actions: tuple[CloseAction, ...]
    applied: bool
    blocked: bool
    cleanup: bool
    github_error: ErrorMessage | None
    github_repository: str | None
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


@dataclass(slots=True)
class _CloseActionRecorder:
    """Collect close actions and track whether any step blocked progress."""

    on_action: Callable[[CloseAction], None] | None
    actions: list[CloseAction] = field(default_factory=list)
    blocked: bool = False

    def record(self, action: CloseAction) -> None:
        if action.status == "blocked":
            self.blocked = True
        self.actions.append(action)
        if self.on_action is not None:
            self.on_action(action)

    def as_tuple(self) -> tuple[CloseAction, ...]:
        return tuple(self.actions)


@dataclass(frozen=True, slots=True)
class _CloseExecutionState:
    """Local tracking state and commit lookup used during close execution."""

    current_state: ReviewState
    next_changes: dict[str, CachedChange]
    commit_ids_by_change_id: dict[str, str]


@dataclass(frozen=True, slots=True)
class _CloseCleanupContext:
    """Shared dependencies for bookmark and stack-comment cleanup."""

    github_client: GithubClient
    github_repository: ParsedGithubRepo
    next_changes: dict[str, CachedChange]
    prepared_close: PreparedClose
    record_action: Callable[[CloseAction], None]
    remote_name: str | None
    revision: ReviewStatusRevision
    revision_label: CloseActionBody

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


def close(
    *,
    cleanup: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `close`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    command = "close --cleanup" if cleanup else "close"
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command=command,
    ):
        return _run_close(
            context=context,
            cleanup=cleanup,
            dry_run=dry_run,
            pull_request=pull_request,
            revset=revset,
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
                github_client_builder=build_github_client,
                github_repo_parser=parse_github_repo,
                pull_request_number=target.pull_request_number,
                state=target.state,
            )
        )
    if isinstance(target, _CloseUntrackedPullRequestTarget):
        return asyncio.run(
            run_untracked_cleanup_pull_request(
                context=context,
                dry_run=dry_run,
                github_client_builder=build_github_client,
                github_repo_parser=parse_github_repo,
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
            action_name="close",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(
            t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}"
        )
        return _CloseSelectedStack(revset=resolved_revset)

    return _CloseSelectedStack(
        revset=resolve_selected_revset(
            command_label=_close_command_label(cleanup=cleanup, dry_run=dry_run),
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        )
    )


def _close_command_label(*, cleanup: bool, dry_run: bool) -> str:
    if cleanup and dry_run:
        return "close --cleanup --dry-run"
    if cleanup:
        return "close --cleanup"
    if dry_run:
        return "close --dry-run"
    return "close"


def print_close_result(result: CloseResult) -> None:
    if result.remote is None:
        console.warning(remote_unavailable_message(remote_error=result.remote_error))
    github_message = github_unavailable_message(
        github_error=result.github_error,
        github_repository=result.github_repository,
    )
    if github_message is not None:
        console.warning(github_message)
    if result.actions:
        if result.blocked:
            header = "Close blocked:"
        elif result.applied:
            header = "Applied close actions:"
        else:
            header = "Planned close actions:"
        console.output(header)
        for action in result.actions:
            prefix, prefix_style, body_style = _close_action_presentation(action.status)
            console.output(
                ui.prefixed_line(
                    f"{prefix} ",
                    _render_close_action_message(action),
                    prefix_labels=prefix_style,
                    message_labels=body_style,
                )
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

    Both plain `close` and `close --cleanup` are true no-ops when the selected
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

    remotes = client.list_git_remotes()
    remote: GitRemote | None = None
    remote_error: ErrorMessage | None = None
    if remotes:
        try:
            remote = select_submit_remote(remotes)
        except CliError as error:
            remote_error = error_message(error)

    github_repository = None
    github_repository_error = None
    if remote is not None:
        github_repository = parse_github_repo(remote)
        if github_repository is None:
            github_repository_error = (
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(remote.name)}. Use a GitHub remote URL."
            )

    prepared = PreparedStack(
        bookmark_states={},
        bookmark_result_changed=False,
        client=client,
        remote=remote,
        remote_error=remote_error,
        stack=stack,
        state=state,
        state_changes=dict(state.changes),
        state_store=state_store,
        status_revisions=tuple(status_revisions),
    )
    return PreparedStatus(
        github_repository=github_repository,
        github_repository_error=github_repository_error,
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
    status_result,
) -> CloseResult:
    prepared_status = prepared_close.prepared_status
    github_repository = prepared_status.github_repository

    recorder = _CloseActionRecorder(on_action=on_action)

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
                    "cannot close pull requests tracked by jj-review without live "
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

    execution_state = _prepare_close_execution_state(prepared_close=prepared_close)
    completed = False
    close_journal: OperationJournal | None = None
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
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            progress_total = len(status_result.revisions) if on_action is None else 0
            with console.progress(
                description="Processing close actions",
                total=progress_total,
            ) as progress:
                blocked = await _process_close_revisions(
                    execution_state=execution_state,
                    github_client=github_client,
                    github_repository=github_repository,
                    on_revision_complete=progress.advance,
                    prepared_close=prepared_close,
                    recorder=recorder,
                    revisions=status_result.revisions,
                )

        _save_close_progress(
            execution_state=execution_state,
            prepared_close=prepared_close,
        )
        completed = True
        return _close_result(
            actions=recorder.as_tuple(),
            blocked=blocked or recorder.blocked,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )
    finally:
        if completed and close_journal is not None:
            completed_change_ids = tuple(
                prepared_revision.revision.change_id
                for prepared_revision in prepared_status.prepared.status_revisions
            )
            close_journal.append(
                "completed",
                {"ordered_change_ids": completed_change_ids},
            )


def _inspected_close_has_no_work(*, revisions) -> bool:
    """Whether close has nothing to do for the inspected revisions.

    Both plain close and cleanup only act on changes jj-review tracks: closing
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


def _prepare_close_execution_state(*, prepared_close: PreparedClose) -> _CloseExecutionState:
    """Load local tracking state and commit IDs once before close execution."""

    prepared_status = prepared_close.prepared_status
    prepared = prepared_status.prepared
    current_state = prepared.state_store.load() if not prepared_close.dry_run else prepared.state
    return _CloseExecutionState(
        current_state=current_state,
        next_changes=dict(current_state.changes),
        commit_ids_by_change_id={
            prepared_revision.revision.change_id: prepared_revision.revision.commit_id
            for prepared_revision in prepared_status.prepared.status_revisions
        },
    )


def _save_close_progress(
    *,
    execution_state: _CloseExecutionState,
    prepared_close: PreparedClose,
) -> None:
    """Persist saved close state when a live run changed tracked metadata."""

    prepared = prepared_close.prepared_status.prepared
    current_state = execution_state.current_state
    if not prepared_close.dry_run and execution_state.next_changes != current_state.changes:
        prepared.state_store.save(
            current_state.model_copy(update={"changes": execution_state.next_changes})
        )


def _start_close_operation_log(
    *,
    prepared_close: PreparedClose,
) -> OperationJournal | None:
    """Write close operation log metadata for live runs."""

    if prepared_close.dry_run:
        return None

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
        operation="close",
        options={"cleanup": prepared_close.cleanup},
        resolved_scope={
            "ordered_change_ids": ordered_change_ids,
            "ordered_commit_ids": ordered_commit_ids,
            "selected_revset": prepared_status.selected_revset,
        },
    )
    return journal


async def _process_close_revisions(
    *,
    execution_state: _CloseExecutionState,
    github_client: GithubClient,
    github_repository,
    on_revision_complete: Callable[[], None] | None,
    prepared_close: PreparedClose,
    recorder: _CloseActionRecorder,
    revisions,
) -> bool:
    """Process each revision in order, stopping on the first fail-closed block."""

    for revision in revisions:
        should_stop = await _process_close_revision(
            change_status=classify_review_status_revision(revision),
            commit_id=execution_state.commit_ids_by_change_id.get(revision.change_id),
            current_state=execution_state.current_state,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=execution_state.next_changes,
            prepared_close=prepared_close,
            record_action=recorder.record,
            revision=revision,
        )
        if on_revision_complete is not None:
            on_revision_complete()
        if should_stop:
            return True
    return False


def _close_result(
    *,
    actions: tuple[CloseAction, ...],
    applied: bool | None = None,
    blocked: bool,
    github_error: ErrorMessage | None,
    github_repository,
    prepared_close: PreparedClose,
) -> CloseResult:
    prepared = prepared_close.prepared_status.prepared
    return CloseResult(
        actions=actions,
        applied=(not prepared_close.dry_run) if applied is None else applied,
        blocked=blocked,
        cleanup=prepared_close.cleanup,
        github_error=github_error,
        github_repository=github_repository.full_name if github_repository else None,
        remote=prepared.remote,
        remote_error=prepared.remote_error,
        selected_revset=prepared_close.prepared_status.selected_revset,
    )


async def _process_close_revision(
    *,
    change_status: ReviewChangeStatus,
    commit_id: str | None,
    current_state,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision: ReviewStatusRevision,
) -> bool:
    lookup = revision.pull_request_lookup
    if lookup is None and not change_status.has_pull_request_lookup_failure:
        return False
    if change_status.pr_lifecycle == "ambiguous" or change_status.has_pull_request_lookup_failure:
        body = (
            lookup.message
            if lookup is not None and lookup.message is not None
            else "cannot safely determine the pull request for this path"
        )
        record_action(
            CloseAction(
                kind="close",
                body=body,
                status="blocked",
            )
        )
        return True

    cached_change = revision.cached_change or current_state.changes.get(revision.change_id)
    revision_label = t"{revision.subject} ({ui.change_id(revision.change_id)})"
    if change_status.pr_lifecycle == "missing":
        return await _process_missing_close_revision(
            cached_change=cached_change,
            commit_id=commit_id,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=next_changes,
            prepared_close=prepared_close,
            record_action=record_action,
            revision=revision,
            revision_label=revision_label,
        )

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
        await _process_open_close_revision(
            cached_change=cached_change,
            commit_id=commit_id,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=next_changes,
            prepared_close=prepared_close,
            pull_request_number=lookup.pull_request.number,
            record_action=record_action,
            revision=revision,
            revision_label=revision_label,
        )
        return False
    if change_status.pr_lifecycle not in {"closed", "merged"}:
        return False

    await _process_closed_close_revision(
        cached_change=cached_change,
        change_status=change_status,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    return False


async def _process_missing_close_revision(
    *,
    cached_change: CachedChange | None,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: CloseActionBody,
) -> bool:
    if cached_change is not None and cached_change.pr_state == "open":
        record_action(
            CloseAction(
                kind="close",
                body=(
                    t"cannot close {revision_label} because GitHub no longer reports a "
                    t"pull request for its branch; run {ui.cmd('status --fetch')} or "
                    t"{ui.cmd('relink')} before retrying"
                ),
                status="blocked",
            )
        )
        return True
    if (
        not prepared_close.cleanup
        or cached_change is None
        or not _has_retirable_cached_review_identity(cached_change)
    ):
        return False

    updated_change = _record_retired_cached_change(
        cached_change=cached_change,
        next_changes=next_changes,
        pr_state=cached_change.pr_state or "closed",
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_if_requested(
        cached_change=updated_change,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    return False


async def _process_open_close_revision(
    *,
    cached_change: CachedChange,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    pull_request_number: int,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: CloseActionBody,
) -> None:
    record_action(
        CloseAction(
            kind="pull request",
            body=t"close PR #{pull_request_number} for {revision_label}",
            status="planned" if prepared_close.dry_run else "applied",
        )
    )
    if not prepared_close.dry_run:
        await github_client.close_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )

    updated_change = _record_retired_cached_change(
        cached_change=cached_change,
        next_changes=next_changes,
        pr_state="closed",
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_if_requested(
        cached_change=updated_change,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )


async def _process_closed_close_revision(
    *,
    cached_change: CachedChange,
    change_status: ReviewChangeStatus,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: CloseActionBody,
) -> None:
    lookup = revision.pull_request_lookup
    pr_state = (
        "merged"
        if (
            lookup is not None
            and lookup.pull_request is not None
            and change_status.pr_lifecycle == "merged"
        )
        else "closed"
    )
    if cached_change.pr_state == "merged":
        pr_state = "merged"

    updated_change = _record_retired_cached_change(
        cached_change=cached_change,
        next_changes=next_changes,
        pr_state=pr_state,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_if_requested(
        cached_change=updated_change,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )


def _record_retired_cached_change(
    *,
    cached_change: CachedChange,
    next_changes: dict[str, CachedChange],
    pr_state: str,
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: CloseActionBody,
) -> CachedChange:
    updated_change = _retire_cached_change(cached_change, pr_state=pr_state)
    if updated_change != cached_change:
        next_changes[revision.change_id] = updated_change
        record_action(
            CloseAction(
                kind="tracking",
                body=t"stop review tracking for {revision_label}",
                status="planned" if prepared_close.dry_run else "applied",
            )
        )
    return updated_change


async def _cleanup_if_requested(
    *,
    cached_change: CachedChange,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: CloseActionBody,
) -> None:
    if not prepared_close.cleanup:
        return
    prepared = prepared_close.prepared_status.prepared
    remote = prepared.remote
    cleanup_context = _CloseCleanupContext(
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        remote_name=remote.name if remote is not None else None,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_revision(
        bookmark_state=prepared.client.get_bookmark_state(revision.bookmark),
        cached_change=cached_change,
        commit_id=commit_id,
        context=cleanup_context,
    )


async def _cleanup_revision(
    *,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    commit_id: str | None,
    context: _CloseCleanupContext,
) -> None:
    bookmark = cached_change.bookmark
    cleanup_plan = _plan_review_bookmark_cleanup(
        bookmark=bookmark,
        cached_change=cached_change,
        bookmark_state=bookmark_state,
        commit_id=commit_id,
        context=context,
    )
    if bookmark is not None:
        apply_bookmark_cleanup(
            bookmark=bookmark,
            cleanup_plan=cleanup_plan,
            commit_id=commit_id,
            record_action=context.record_action,
            remote_name=context.remote_name,
            run=context,
        )

    if cached_change.pr_number is None:
        return

    cleared_comment = False
    for kind, cached_comment_id in (
        ("navigation", cached_change.navigation_comment_id),
        ("overview", cached_change.overview_comment_id),
    ):
        comment, comment_error = await _find_managed_comment(
            cached_comment_id=cached_comment_id,
            github_client=context.github_client,
            github_repository=context.github_repository,
            kind=kind,
            pull_request_number=cached_change.pr_number,
        )
        if comment_error is not None:
            context.record_action(comment_error)
            return
        if comment is None:
            continue
        cleared_comment = True
        context.record_action(
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"delete {stack_comment_label(kind)} #{comment.id} from PR "
                    f"#{cached_change.pr_number}"
                ),
                status="planned" if context.dry_run else "applied",
            )
        )
        if not context.dry_run:
            await context.github_client.delete_issue_comment(
                context.github_repository.owner,
                context.github_repository.repo,
                comment_id=comment.id,
            )

    if (
        cached_change.navigation_comment_id is not None
        or cached_change.overview_comment_id is not None
        or cleared_comment
    ):
        context.next_changes[context.revision.change_id] = (
            cached_change.with_cleared_comments()
        )


def _plan_review_bookmark_cleanup(
    *,
    bookmark: str | None,
    cached_change: CachedChange,
    bookmark_state: BookmarkState,
    commit_id: str | None,
    context: _CloseCleanupContext,
) -> _BookmarkCleanupPlan:
    if bookmark is None:
        return _BookmarkCleanupPlan(local_forget=False, remote_delete=False)
    return plan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        cached_change=cached_change,
        cleanup_user_bookmarks=context.cleanup_user_bookmarks,
        commit_id=commit_id,
        prefix=context.bookmark_prefix,
        record_action=context.record_action,
        remote_name=context.remote_name,
    )


def _has_retirable_cached_review_identity(cached_change: CachedChange) -> bool:
    """Return True when tracking state proves prior review identity."""

    return classify_saved_review_change(
        cached_change,
        local="present",
    ).saved_review_identity
