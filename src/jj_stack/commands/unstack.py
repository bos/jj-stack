"""Close or locally forget the selected stack.

Passing `--cleanup` also removes `jj-stack`'s own review branches, forgets any local bookmarks
that still point at those branches, and clears saved tracking data for the selected stack.

If you asked `jj-stack` to use your own bookmarks with `submit --use-bookmarks`, those are
preserved unless `cleanup_user_bookmarks = true`. Use `--pull-request` to close by PR number or
URL.

Use `unstack --cleanup --pull-request <pr>` to retire an orphaned PR shown by `list`.
Use `unstack --cleanup --pull-request orphans` to retire every orphan shown by `list`.
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
    retire_review_identity,
)
from jj_stack.commands.close_orphan import (
    run_orphan_close,
    run_untracked_cleanup_pull_request,
    state_has_pull_request_record,
)
from jj_stack.errors import AmbiguousSelectionError, CliError, ErrorMessage, UsageError
from jj_stack.github.client import GithubClient, build_github_client
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.resolution import (
    GithubRepoAddress,
    resolve_github_target,
)
from jj_stack.github.stack_comments import stack_comment_label
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
    enumerate_orphaned_records,
)
from jj_stack.review.discovery import discover_tracked_stacks
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
    review_identities: dict[str, ReviewIdentity]
    prepared_close: PreparedClose
    record_action: Callable[[CloseAction], None]

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

    close_orphans = pull_request == "orphans"
    if close_orphans and not cleanup:
        raise UsageError("unstack --pull-request orphans requires --cleanup.")
    if close_orphans and local:
        raise UsageError("unstack --pull-request orphans cannot be combined with --local.")
    if close_orphans and revset is not None:
        raise UsageError("unstack --pull-request orphans cannot be combined with a revision.")
    if local and cleanup:
        raise UsageError("unstack --local cannot be combined with --cleanup.")
    if close_orphans:
        command = "unstack --cleanup --pull-request orphans"
    elif local:
        command = "unstack --local"
    elif cleanup:
        command = "unstack --cleanup"
    else:
        command = "unstack"
    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
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
        if close_orphans:
            return asyncio.run(
                _run_orphan_closes(
                    context=context,
                    dry_run=dry_run,
                )
            )
        return _run_close(
            context=context,
            cleanup=cleanup,
            dry_run=dry_run,
            pull_request=pull_request,
            revset=revset,
        )


async def _run_orphan_closes(
    *,
    context: CommandContext,
    dry_run: bool,
) -> int:
    state = context.state_store.load()
    discovered = discover_tracked_stacks(
        jj_client=context.jj_client,
        state=state,
    )
    orphaned_records = enumerate_orphaned_records(state, discovered.stacks)
    orphan_targets_by_pull_request: dict[int, list[str]] = {}
    for orphan in orphaned_records:
        pull_request_number = orphan.review_identity.pr_number
        orphan_targets_by_pull_request.setdefault(pull_request_number, []).append(
            orphan.change_id
        )

    active_claims_by_pull_request = {
        pull_request_number: [
            change_id
            for change_id, review_identity in state.review_identities.items()
            if review_identity.pr_number == pull_request_number and review_identity.is_tracked
        ]
        for pull_request_number in orphan_targets_by_pull_request
    }
    ambiguous_targets = {
        pull_request_number: change_ids
        for pull_request_number, change_ids in active_claims_by_pull_request.items()
        if len(change_ids) > 1
    }
    if ambiguous_targets:
        details = ", ".join(
            f"PR #{pull_request_number} ({', '.join(change_id[:8] for change_id in change_ids)})"
            for pull_request_number, change_ids in sorted(ambiguous_targets.items())
        )
        raise AmbiguousSelectionError(
            t"Cannot retire orphaned pull requests because multiple tracking records claim "
            t"the same PR: {details}.",
            hint=t"Repair the tracking data with {ui.cmd('unlink')} or {ui.cmd('relink')} "
            t"before retrying.",
        )

    targets = tuple(
        (pull_request_number, change_ids[0])
        for pull_request_number, change_ids in sorted(orphan_targets_by_pull_request.items())
    )
    if not targets:
        console.output("No orphaned pull requests are tracked.")
        return 0

    blocked = False
    for pull_request_number, change_id in targets:
        current_state = context.state_store.load()
        if change_id not in current_state.submitted_baselines:
            console.warning(
                t"Cannot retire PR #{pull_request_number}: its submitted baseline is "
                t"unavailable; run {ui.cmd('relink')} to repair the saved review."
            )
            blocked = True
            continue
        exit_code = await run_orphan_close(
            change_id=change_id,
            context=context,
            dry_run=dry_run,
            pull_request_number=pull_request_number,
            state=current_state,
        )
        blocked = blocked or exit_code != 0
    return 1 if blocked else 0


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
    actions: list[LocalUnstackAction] = []
    retirements: list[tuple[str, ReviewIdentity, SubmittedBaseline]] = []
    for revision in stack.revisions:
        review_identity = state.review_identities.get(revision.change_id)
        submitted_baseline = state.submitted_baselines.get(revision.change_id)
        if review_identity is None and submitted_baseline is None:
            continue
        if review_identity is None or submitted_baseline is None:
            raise CliError(
                t"Cannot forget tracking for {ui.change_id(revision.change_id)} because its "
                t"saved identity or submitted baseline is unavailable.",
                hint=t"Run {ui.cmd('relink')} to repair the saved review before retrying.",
            )
        retirements.append((revision.change_id, review_identity, submitted_baseline))
        actions.append(
            LocalUnstackAction(
                bookmark=review_identity.head_ref,
                change_id=revision.change_id,
                subject=revision.subject,
            )
        )
    if actions and not dry_run:
        for change_id, review_identity, submitted_baseline in retirements:
            context.state_store.retire_review(
                change_id,
                expected_identity=review_identity,
                expected_baseline=submitted_baseline,
            )
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
        review_identity = state.review_identities.get(revision.change_id)
        if review_identity is not None:
            return None
        status_revisions.append(
            PreparedRevision(
                bookmark="",
                bookmark_source="generated",
                revision=revision,
                review_identity=None,
                submitted_baseline=state.submitted_baselines.get(revision.change_id),
            )
        )

    github_target = resolve_github_target(client.list_git_remotes())

    prepared = PreparedStack(
        bookmark_states={},
        client=client,
        remote=github_target.remote,
        remote_error=github_target.remote_error,
        stack=stack,
        state=state,
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

    current_state = (
        prepared_close.context.state_store.load()
        if not prepared_close.dry_run
        else prepared.state
    )
    if no_work:
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
            review_identities=dict(current_state.review_identities),
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

    return _close_result(
        actions=recorder.as_tuple(),
        blocked=blocked or recorder.blocked,
        github_error=status_result.github_error,
        github_repository=github_repository,
        prepared_close=prepared_close,
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

    return not any(
        revision.review_identity is not None or revision.submitted_baseline is not None
        for revision in revisions
    )


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

    review_identity = (
        revision.review_identity if run.dry_run else run.review_identities.get(revision.change_id)
    )
    submitted_baseline = (
        revision.submitted_baseline
        if run.dry_run
        else run.current_state.submitted_baselines.get(revision.change_id)
    )
    if review_identity is None or submitted_baseline is None:
        if review_identity is None and submitted_baseline is None:
            return False
        run.record_action(
            CloseAction(
                kind="tracking",
                body=t"cannot close {ui.change_id(revision.change_id)} because its saved "
                t"identity or submitted baseline is unavailable; run {ui.cmd('relink')} "
                t"before retrying",
                status="blocked",
            )
        )
        return True

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

    revision_label = t"{revision.subject} ({ui.change_id(revision.change_id)})"

    if change_status.pr_lifecycle == "missing":
        if not review_identity.is_unlinked:
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
        if not run.prepared_close.cleanup or not change_status.saved_review_identity:
            return False
    else:
        if lookup is None:
            return False
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
                await run.github_client.close_pull_request(
                    pull_number=pull_request_number,
                )
        elif change_status.pr_lifecycle in {"closed", "merged"}:
            pass
        else:
            return False

    updated_identity = _record_retired_review_identity(
        review_identity=review_identity,
        revision=revision,
        revision_label=revision_label,
        run=run,
    )
    if not run.prepared_close.cleanup:
        _persist_retired_review_identity(
            change_id=revision.change_id,
            previous_identity=review_identity,
            updated_identity=updated_identity,
            run=run,
        )
        return False
    bookmark_states = run.prepared_close.prepared_status.prepared.bookmark_states
    await _cleanup_revision(
        bookmark_state=bookmark_states.get(
            revision.bookmark,
            BookmarkState(name=revision.bookmark),
        ),
        review_identity=updated_identity,
        commit_id=run.commit_ids_by_change_id.get(revision.change_id),
        revision=revision,
        run=run,
    )
    _persist_retired_review_identity(
        change_id=revision.change_id,
        previous_identity=review_identity,
        updated_identity=updated_identity,
        run=run,
    )
    return False


def _record_retired_review_identity(
    *,
    review_identity: ReviewIdentity,
    revision: ReviewStatusRevision,
    revision_label: CloseActionBody,
    run: _CloseMutationRun,
) -> ReviewIdentity:
    updated_identity = retire_review_identity(review_identity)
    if updated_identity != review_identity:
        run.record_action(
            CloseAction(
                kind="tracking",
                body=t"stop review tracking for {revision_label}",
                status="planned" if run.dry_run else "applied",
            )
        )
    return updated_identity


def _persist_retired_review_identity(
    *,
    change_id: str,
    previous_identity: ReviewIdentity,
    updated_identity: ReviewIdentity,
    run: _CloseMutationRun,
) -> None:
    if run.dry_run or updated_identity == previous_identity:
        return
    run.prepared_close.context.state_store.set_link_state(
        change_id,
        expected_identity=previous_identity,
        link_state="unlinked",
    )
    run.review_identities[change_id] = updated_identity


async def _cleanup_revision(
    *,
    bookmark_state: BookmarkState,
    review_identity: ReviewIdentity,
    commit_id: str | None,
    revision: ReviewStatusRevision,
    run: _CloseMutationRun,
) -> None:
    bookmark = review_identity.head_ref
    cleanup_plan = plan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        cleanup_user_bookmarks=run.cleanup_user_bookmarks,
        commit_id=commit_id,
        prefix=run.bookmark_prefix,
        record_action=run.record_action,
        remote_name=run.remote_name,
        review_identity=review_identity,
    )
    apply_bookmark_cleanup(
        bookmark=bookmark,
        cleanup_plan=cleanup_plan,
        commit_id=commit_id,
        record_action=run.record_action,
        remote_name=run.remote_name,
        run=run,
    )

    lookups = await _find_managed_comments(
        github_client=run.github_client,
        pull_request_number=review_identity.pr_number,
    )
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
        run.record_action(
            CloseAction(
                kind=stack_comment_label(lookup.kind),
                body=(
                    f"delete {stack_comment_label(lookup.kind)} #{lookup.comment.id} from PR "
                    f"#{review_identity.pr_number}"
                ),
                status="planned" if run.dry_run else "applied",
            )
        )
        if not run.dry_run:
            await run.github_client.delete_issue_comment(
                comment_id=lookup.comment.id,
            )
