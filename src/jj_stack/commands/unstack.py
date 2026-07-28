"""End review for a stack without changing its local jj changes.

With no mode flag, `unstack` closes the tracked open pull requests but keeps their review
branches and tracking so later cleanup remains safe. Passing `--cleanup` also
removes review branches, comments, and tracking that `jj-stack` can verify are safe to delete.
Use `--pull-request` to close by PR number or URL.

Use `jj-stack unstack --cleanup --pull-request <pr>` to close and clean up an orphaned PR shown
by `list`. Use `jj-stack unstack --cleanup --pull-request orphans` to clean up every orphan
shown by `list`. Use `jj-stack unstack --local` to forget local review tracking without closing
PRs or deleting branches.

Common examples: `jj-stack unstack --dry-run` previews which PRs would close;
`jj-stack unstack --local` forgets only local review tracking; and
`jj-stack unstack --cleanup --pull-request orphans` closes and cleans up every orphan shown by
`list`.
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
    apply_managed_comment_cleanup,
    apply_remote_branch_cleanup,
    check_current_review_cleanup,
    check_tracked_review,
    close_current_tracked_pull_request,
    emit_close_actions,
    find_managed_comments as _find_managed_comments,
    github_observation_blocker,
    plan_review_cleanup,
    prepare_current_review_cleanup,
)
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.commands.close_orphan import (
    run_orphan_close,
    run_untracked_cleanup_pull_request,
    state_has_pull_request_record,
)
from jj_stack.errors import AmbiguousSelectionError, CliError, ErrorMessage, UsageError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.resolution import (
    GithubRepoAddress,
    resolve_github_target,
)
from jj_stack.github.stack_comments import stack_comment_label
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.branches import review_branch_matches_change
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
    enumerate_orphaned_records,
)
from jj_stack.review.discovery import discover_tracked_stacks
from jj_stack.review.observation import RepositoryObservation, observe_reviews
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

    branch: str
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

    current_state: ReviewState
    github_client: GithubClient
    initial_observation: RepositoryObservation | None
    planned_closed_pull_requests: set[int]
    review_identities: dict[str, ReviewIdentity]
    prepared_close: PreparedClose
    record_action: Callable[[CloseAction], None]

    @property
    def dry_run(self) -> bool:
        return self.prepared_close.dry_run

    @property
    def cleanup_retry_command(self) -> str:
        head = self.prepared_close.prepared_status.prepared.stack.head
        return f"unstack --cleanup {head.change_id}"

    @property
    def jj_client(self) -> JjClient:
        return self.prepared_close.prepared_status.prepared.client

    @property
    def remote_name(self) -> str:
        remote = self.prepared_close.prepared_status.prepared.remote
        if remote is None:
            raise AssertionError("Cleanup requires a configured remote.")
        return remote.name


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
            if review_identity.pr_number == pull_request_number
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
            t"Cannot clean up orphaned pull requests because multiple tracking records claim "
            t"the same PR: {details}.",
            hint=t"Discard an incorrect claim with {ui.cmd('unstack --local')} or repair it "
            t"with {ui.cmd('relink')} before retrying.",
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
                t"Cannot clean up PR #{pull_request_number}: its last submitted commit is "
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
                t"saved pull request details or last submitted commit are unavailable.",
                hint=t"Run {ui.cmd('relink')} to repair the saved review before retrying.",
            )
        retirements.append((revision.change_id, review_identity, submitted_baseline))
        actions.append(
            LocalUnstackAction(
                branch=review_identity.head_ref,
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
        default_revset=None,
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
        console.output(
            t"  {icon} tracking: forget local review tracking for {revision_label}, "
            t"preserving {ui.bookmark(action.branch)}"
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
            default_revset=None,
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
    prepared_status = prepare_status(
        context=context,
        fetch_remote_state=False,
        fetch_only_when_tracked=True,
        revset=revset,
    )
    remote = prepared_status.prepared.remote
    if cleanup and remote is not None:
        context.jj_client.ensure_review_fetch_isolation(
            remote=remote.name,
            dry_run=dry_run,
            on_change=report_fetch_isolation,
        )
    return PreparedClose(
        cleanup=cleanup,
        context=context,
        dry_run=dry_run,
        prepared_status=prepared_status,
    )


def _prepare_untracked_close_fast_path(
    *,
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus | None:
    """Build the no-op close path without remote review discovery.

    Both plain `unstack` and `unstack --cleanup` are true no-ops when the selected
    stack has no saved review identity at all. In that case we can skip
    remote review discovery and GitHub preparation while preserving the normal
    remote diagnostics and stale-operation retirement behavior.
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
                branch=None,
                revision=revision,
                review_identity=None,
                submitted_baseline=state.submitted_baselines.get(revision.change_id),
            )
        )

    github_target = resolve_github_target(client.list_git_remotes())

    prepared = PreparedStack(
        client=client,
        remote=github_target.remote,
        remote_error=github_target.remote_error,
        remote_targets={},
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
    async with build_github_client(repository=github_repository) as github_client:
        review_identities = dict(current_state.review_identities)
        selection = GithubStackSelection(
            github_client,
            tuple(
                review_identities[prepared_revision.revision.change_id].pr_number
                for prepared_revision in prepared.status_revisions
                if prepared_revision.revision.change_id in review_identities
            ),
            prepared_close.context.state_store,
        )
        native_stacks = await selection.active_stacks(persist=not prepared_close.dry_run)
        initial_observation = None
        blocked = False
        if native_stacks or prepared_close.cleanup:
            if prepared.remote is None:
                raise AssertionError("Tracked unstack requires a configured remote.")
            try:
                initial_observation = await observe_reviews(
                    change_ids=tuple(revision.change_id for revision in status_result.revisions),
                    context=prepared_close.context,
                    github_client=github_client,
                    include_open_dependents=prepared_close.cleanup,
                    remote_name=prepared.remote.name,
                )
            except GithubClientError:
                recorder.record(github_observation_blocker())
                blocked = True
        run = _CloseMutationRun(
            current_state=current_state,
            github_client=github_client,
            initial_observation=initial_observation,
            planned_closed_pull_requests=set(),
            review_identities=review_identities,
            prepared_close=prepared_close,
            record_action=recorder.record,
        )
        if native_stacks and not blocked:
            native_preflight = (
                _close_revision_preflight_error(
                    change_status=classify_review_status_revision(revision),
                    revision=revision,
                    run=run,
                )
                for revision in status_result.revisions
            )
            blocker = next(
                (action for action in native_preflight if action is not None),
                None,
            )
            if blocker is None:
                assert initial_observation is not None
                blocker = _selected_observation_blocker(
                    observation=initial_observation,
                    revisions=status_result.revisions,
                    run=run,
                )
            if blocker is not None:
                recorder.record(blocker)
                blocked = True
            else:
                native_stack = (
                    await selection.recheck_active_suffix(observed=native_stacks, persist=False)
                    if prepared_close.dry_run
                    else await selection.dissolve_exact(observed=native_stacks)
                )
                if native_stack is not None:
                    recorder.record(
                        CloseAction(
                            kind="GitHub stack",
                            body=t"dissolve GitHub stack #{native_stack.number}",
                            status="planned" if prepared_close.dry_run else "applied",
                        )
                    )
        progress_total = len(status_result.revisions) if on_action is None else 0
        with console.progress(
            description="Processing close actions",
            total=progress_total,
        ) as progress:
            for revision in () if blocked else status_result.revisions:
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
    a saved pull request or deleting its remote branch. Neither action is
    possible for a change without review identity, so either variant is a
    true no-op on such a stack.
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


def _selected_observation_blocker(
    *,
    observation: RepositoryObservation,
    revisions: tuple[ReviewStatusRevision, ...],
    run: _CloseMutationRun,
) -> CloseAction | None:
    """Check every selected tracked PR from one batch observation."""

    for revision in revisions:
        identity = run.review_identities.get(revision.change_id)
        baseline = run.current_state.submitted_baselines.get(revision.change_id)
        if identity is None or baseline is None:
            continue
        _pull_request, blocker = check_tracked_review(
            allowed_states=frozenset({"open", "closed", "merged"}),
            change_id=revision.change_id,
            observation=observation,
            review_identity=identity,
            submitted_baseline=baseline,
        )
        if blocker is not None:
            return blocker
    return None


async def _process_close_revision(
    *,
    change_status: ReviewChangeStatus,
    revision: ReviewStatusRevision,
    run: _CloseMutationRun,
) -> bool:
    """Close one revision's PR, retire its tracking, and clean up when requested."""

    if preflight_error := _close_revision_preflight_error(
        change_status=change_status,
        revision=revision,
        run=run,
    ):
        run.record_action(preflight_error)
        return True
    review_identity = (
        revision.review_identity if run.dry_run else run.review_identities.get(revision.change_id)
    )
    submitted_baseline = (
        revision.submitted_baseline
        if run.dry_run
        else run.current_state.submitted_baselines.get(revision.change_id)
    )
    if review_identity is None or submitted_baseline is None:
        return False
    revision_label = t"{revision.subject} ({ui.change_id(revision.change_id)})"

    observed_pull_request = (
        None
        if revision.pull_request_lookup is None
        else revision.pull_request_lookup.pull_request
    )
    pull_request, close_action = await close_current_tracked_pull_request(
        change_id=revision.change_id,
        dry_run=run.dry_run,
        github_client=run.github_client,
        observed_pull_request=observed_pull_request,
        review_identity=review_identity,
        state_store=run.prepared_close.context.state_store,
        submitted_baseline=submitted_baseline,
        target_label=revision_label,
    )
    if close_action is not None:
        run.record_action(close_action)
        if close_action.status == "blocked":
            return True

    if not run.prepared_close.cleanup:
        return False
    if run.dry_run and pull_request is not None:
        run.planned_closed_pull_requests.add(pull_request.number)
    cleanup_succeeded = await _cleanup_revision(
        review_identity=review_identity,
        revision=revision,
        run=run,
        submitted_baseline=submitted_baseline,
    )
    if not cleanup_succeeded:
        return True
    if not run.dry_run:
        blocker = await check_current_review_cleanup(
            change_id=revision.change_id,
            context=run.prepared_close.context,
            expected_update=None,
            github_client=run.github_client,
            remote_name=run.remote_name,
            retry_command=run.cleanup_retry_command,
            review_identity=review_identity,
            submitted_baseline=submitted_baseline,
        )
        if blocker is not None:
            run.record_action(blocker)
            return True
    _retire_cleaned_review(
        change_id=revision.change_id,
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
        revision_label=revision_label,
        run=run,
    )
    return False


def _close_revision_preflight_error(
    *,
    change_status: ReviewChangeStatus,
    revision: ReviewStatusRevision,
    run: _CloseMutationRun,
) -> CloseAction | None:
    """Return a blocker known before any close mutation for one revision."""

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
            return None
        return CloseAction(
            kind="tracking",
            body=t"cannot close {ui.change_id(revision.change_id)} because its saved pull "
            t"request details or last submitted commit are unavailable; run "
            t"{ui.cmd('relink')} before retrying",
            status="blocked",
        )
    github_repository = run.prepared_close.prepared_status.github_repository
    if (
        github_repository is None
        or review_identity.repository_key != github_repository.repository_key
    ):
        return CloseAction(
            kind="close",
            body=t"cannot close PR #{review_identity.pr_number} because its saved repository "
            t"does not match the configured GitHub repository; run {ui.cmd('relink')} before "
            t"retrying",
            status="blocked",
        )
    lookup = revision.pull_request_lookup
    if change_status.pr_lifecycle == "ambiguous" or change_status.has_pull_request_lookup_failure:
        body = (
            lookup.message
            if lookup is not None and lookup.message is not None
            else "cannot safely determine the pull request for this path"
        )
        return CloseAction(kind="close", body=body, status="blocked")
    pull_request = None if lookup is None else lookup.pull_request
    if pull_request is not None and not review_identity.matches_pull_request(pull_request):
        return CloseAction(
            kind="close",
            body=t"cannot close PR #{pull_request.number} because it is not the exact pull "
            t"request saved for {ui.change_id(revision.change_id)}",
            status="blocked",
        )
    if change_status.pr_lifecycle == "missing":
        revision_label = t"{revision.subject} ({ui.change_id(revision.change_id)})"
        return CloseAction(
            kind="close",
            body=t"cannot close {revision_label} because GitHub no longer reports a pull "
            t"request for its branch; run {ui.cmd('view')} or "
            t"{ui.cmd('relink')} before retrying",
            status="blocked",
        )
    if run.prepared_close.cleanup and not review_branch_matches_change(
        review_identity.head_ref,
        revision.change_id,
    ):
        return CloseAction(
            kind="tracking",
            body=t"cannot clean up {ui.bookmark(review_identity.head_ref)} because it does "
            t"not match change {ui.change_id(revision.change_id)}; run "
            t"{ui.cmd('relink')} before retrying",
            status="blocked",
        )
    return None


def _retire_cleaned_review(
    *,
    change_id: str,
    review_identity: ReviewIdentity,
    submitted_baseline: SubmittedBaseline,
    revision_label: CloseActionBody,
    run: _CloseMutationRun,
) -> None:
    action = CloseAction(
        kind="tracking",
        body=t"stop review tracking for {revision_label}",
        status="planned" if run.dry_run else "applied",
    )
    if run.dry_run:
        run.record_action(action)
        return
    run.prepared_close.context.state_store.retire_review(
        change_id,
        expected_identity=review_identity,
        expected_baseline=submitted_baseline,
    )
    run.review_identities.pop(change_id, None)
    run.record_action(action)


async def _cleanup_revision(
    *,
    review_identity: ReviewIdentity,
    revision: ReviewStatusRevision,
    run: _CloseMutationRun,
    submitted_baseline: SubmittedBaseline,
) -> bool:
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
            return False
    if run.dry_run:
        assert run.initial_observation is not None
        _pull_request, branch_update, blocker = plan_review_cleanup(
            allowed_states=frozenset({"open", "closed", "merged"}),
            change_id=revision.change_id,
            observation=run.initial_observation,
            preview_closed_dependents=frozenset(run.planned_closed_pull_requests),
            retry_command=run.cleanup_retry_command,
            review_identity=review_identity,
            submitted_baseline=submitted_baseline,
        )
    else:
        branch_update, blocker = await prepare_current_review_cleanup(
            allowed_states=frozenset({"closed", "merged"}),
            change_id=revision.change_id,
            context=run.prepared_close.context,
            github_client=run.github_client,
            remote_name=run.remote_name,
            retry_command=run.cleanup_retry_command,
            review_identity=review_identity,
            submitted_baseline=submitted_baseline,
        )
    if blocker is not None:
        run.record_action(blocker)
        return False

    apply_remote_branch_cleanup(
        dry_run=run.dry_run,
        jj_client=run.jj_client,
        record_action=run.record_action,
        remote_name=run.remote_name,
        update=branch_update,
    )
    comment_actions, comments_current = await apply_managed_comment_cleanup(
        change_id=revision.change_id,
        dry_run=run.dry_run,
        github_client=run.github_client,
        lookups=lookups,
        review_identity=review_identity,
        state_store=run.prepared_close.context.state_store,
        submitted_baseline=submitted_baseline,
    )
    for action in comment_actions:
        run.record_action(action)
    return comments_current
