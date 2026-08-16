"""Remove review branches, comments, and saved links no active pull request needs.

With no selector, it checks the whole repository. A revision limits cleanup to one local stack;
`--pull-request` selects one tracked pull request, and `--pull-request orphans` selects every
tracked pull request whose local change is gone. Add `--close` to close selected open pull
requests before cleanup.

Without `--close`, open pull requests are left alone. Already closed or merged pull requests do
not need the flag and are cleaned up normally.

If another open pull request still uses a review branch as its base, that branch stays. Close or
retarget the pull request named in the message, then rerun the same cleanup command.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._cleanup_actions import (
    OverviewCommentLookup,
    ReviewMutationAction,
    apply_overview_comment_cleanup,
    apply_remote_branch_cleanup,
    check_tracked_review,
    emit_action_row,
    find_overview_comment,
    github_stack_cleanup_blocker,
    plan_review_cleanup,
)
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import AmbiguousSelectionError, UsageError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_LABEL
from jj_stack.github.resolution import (
    GithubTarget,
    resolve_github_target,
)
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import ReviewRefUpdate
from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.review.change_status import enumerate_orphaned_records
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_reviews,
)
from jj_stack.review.repository import observe_repository_paths
from jj_stack.review.selected import select_review_path
from jj_stack.review.selection import resolve_pull_request_number
from jj_stack.state.operation_lock import (
    acquire_operation_lock,
)
from jj_stack.ui import plain_text

from .shared import (
    CleanupAction,
    CleanupResult,
    PreparedCleanup,
    PreparedCleanupChange,
)
from .stale import (
    LocalCleanupObservation,
    local_cleanup_observations,
)

HELP = "Remove review data that no active pull request needs"


def _build_action_streamer(*, header: str) -> Callable[[CleanupAction], None]:
    """Print the action header once, then stream actions as they arrive."""

    header_printed = False

    def emit_action(action: CleanupAction) -> None:
        nonlocal header_printed
        if not header_printed:
            console.output(header)
            header_printed = True
        emit_action_row(kind=action.kind, status=action.status, body=action.body)

    return emit_action


def cleanup(
    *,
    cli_args: JjCliArgs,
    close: bool,
    debug: bool,
    dry_run: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    if pull_request is not None and revset is not None:
        raise UsageError("cleanup --pull-request cannot be combined with a revision.")
    if close and pull_request is None:
        raise UsageError("cleanup --close requires --pull-request.")

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="cleanup",
    ):
        return _run_cleanup_command(
            close=close,
            context=context,
            dry_run=dry_run,
            pull_request=pull_request,
            revset=revset,
        )


def _run_cleanup_command(
    *,
    close: bool,
    context: CommandContext,
    dry_run: bool,
    pull_request: str | None,
    revset: str | None,
) -> int:
    """Render and run the stale cleanup command path."""

    with console.spinner(description="Loading review state"):
        prepared_cleanup = _prepare_cleanup(
            close=close,
            context=context,
            dry_run=dry_run,
            pull_request=pull_request,
            revset=revset,
        )
    selected_change_ids = prepared_cleanup.selected_change_ids
    observed_change_ids = (
        tuple(prepared_cleanup.state.review_identities)
        if selected_change_ids is None
        else selected_change_ids
    )
    local_observations = local_cleanup_observations(
        change_ids=observed_change_ids,
        context=prepared_cleanup.context,
    )
    if _cleanup_needs_remote_context(prepared_cleanup=prepared_cleanup):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        for message in github_target_unavailable_messages(prepared_cleanup.github_target):
            console.warning(plain_text(message))

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                header=(
                    "Planned cleanup actions:"
                    if prepared_cleanup.dry_run
                    else "Applied cleanup actions:"
                ),
            ),
            prepared_cleanup=prepared_cleanup,
            local_observations=local_observations,
        )
    )
    if not result.actions:
        console.output("No cleanup actions needed.")
    return 1 if any(action.status == "blocked" for action in result.actions) else 0


async def cleanup_tracked_reviews(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
    dry_run: bool,
    github_client: GithubClient,
    github_target: GithubTarget,
    planned_detached_dependents: frozenset[int] = frozenset(),
    planned_local_removals: frozenset[str] = frozenset(),
) -> CleanupResult:
    """Run the cleanup implementation for reviews reconciled by another command."""

    state = context.state_store.load()
    if not dry_run:
        context.state_store.require_writable()
    prepared_cleanup = PreparedCleanup(
        close_open_pull_requests=False,
        context=context,
        github_target=github_target,
        dry_run=dry_run,
        selected_change_ids=change_ids,
        state=state,
    )
    local_observations = local_cleanup_observations(
        change_ids=change_ids,
        context=context,
    )
    if dry_run:
        local_observations.update(
            {
                change_id: LocalCleanupObservation(
                    has_mutable_copy=False,
                    stale_reason=None,
                )
                for change_id in planned_local_removals
            }
        )
    return await _run_cleanup_async(
        github_client=github_client,
        on_action=_build_action_streamer(
            header="Planned cleanup actions:" if dry_run else "Applied cleanup actions:",
        ),
        prepared_cleanup=prepared_cleanup,
        preview_detached_dependents=(planned_detached_dependents if dry_run else frozenset()),
        local_observations=local_observations,
    )


def _prepare_cleanup(
    *,
    close: bool,
    context: CommandContext,
    dry_run: bool,
    pull_request: str | None,
    revset: str | None,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    state_store = context.state_store
    state = state_store.load()
    if not dry_run:
        state_store.require_writable()
    selected_change_ids = _resolve_cleanup_change_ids(
        context=context,
        pull_request=pull_request,
        revset=revset,
        state=state,
    )

    return PreparedCleanup(
        close_open_pull_requests=close,
        context=context,
        github_target=None,
        dry_run=dry_run,
        selected_change_ids=selected_change_ids,
        state=state,
    )


def _resolve_cleanup_change_ids(
    *,
    context: CommandContext,
    pull_request: str | None,
    revset: str | None,
    state: ReviewState,
) -> tuple[str, ...] | None:
    """Resolve an optional cleanup selector to saved change IDs."""

    if pull_request == "orphans":
        repository_paths = observe_repository_paths(
            jj_client=context.jj_client,
            namespace=context.review_namespace,
            state=state,
        )
        tracked_stacks = tuple(
            path.stack for path in repository_paths.paths if path.tracked_change_ids
        )
        return tuple(
            orphan.change_id for orphan in enumerate_orphaned_records(state, tracked_stacks)
        )
    if pull_request is not None:
        pull_request_number = resolve_pull_request_number(
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
        )
        matches = tuple(
            change_id
            for change_id, identity in state.review_identities.items()
            if identity.pr_number == pull_request_number
        )
        if len(matches) > 1:
            raise AmbiguousSelectionError(
                t"Multiple saved reviews claim PR #{pull_request_number}.",
                hint=t"Run {ui.cmd('list')} to inspect them and repair the incorrect link.",
            )
        return matches
    if revset is None:
        return None
    stack = select_review_path(
        jj_client=context.jj_client,
        namespace=context.review_namespace,
        revset=revset,
        state=state,
    ).stack
    return tuple(
        revision.change_id
        for revision in stack.revisions
        if revision.change_id in state.review_identities
    )


async def _run_cleanup_async(
    *,
    github_client: GithubClient | None = None,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int] = frozenset(),
    local_observations: dict[str, LocalCleanupObservation],
) -> CleanupResult:
    actions: list[CleanupAction] = []

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    prepared_changes = _run_local_cleanup_pass(
        prepared_cleanup=prepared_cleanup,
        local_observations=local_observations,
    )
    github_target = prepared_cleanup.github_target
    if isinstance(github_target, GithubTarget) and prepared_changes:
        if github_client is not None:
            await _run_tracked_review_cleanup_pass(
                github_client=github_client,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                preview_detached_dependents=preview_detached_dependents,
                record_action=record_action,
            )
        else:
            async with build_github_client(repository=github_target.repository) as client:
                await _run_tracked_review_cleanup_pass(
                    github_client=client,
                    prepared_changes=prepared_changes,
                    prepared_cleanup=prepared_cleanup,
                    preview_detached_dependents=preview_detached_dependents,
                    record_action=record_action,
                )
    elif prepared_changes:
        for prepared_change in prepared_changes:
            candidate = prepared_change.candidate
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect PR #{candidate.review_identity.pr_number} for "
                    t"{ui.change_id(candidate.change_id)} because the GitHub repository "
                    t"cannot be resolved",
                )
            )
    return CleanupResult(actions=tuple(actions))


def _run_local_cleanup_pass(
    *,
    prepared_cleanup: PreparedCleanup,
    local_observations: dict[str, LocalCleanupObservation],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    selected_change_ids = prepared_cleanup.selected_change_ids
    change_ids = (
        tuple(prepared_cleanup.state.review_identities)
        if selected_change_ids is None
        else selected_change_ids
    )
    for change_id in change_ids:
        candidate = prepared_cleanup.state.tracked_review(change_id)
        if candidate is None:
            continue
        local_observation = local_observations.get(
            change_id,
            LocalCleanupObservation(
                has_mutable_copy=False,
                stale_reason="local change was not inspected",
            ),
        )
        prepared_changes.append(
            PreparedCleanupChange(
                candidate=candidate,
                has_mutable_copy=local_observation.has_mutable_copy,
                stale_reason=local_observation.stale_reason,
            )
        )
    return tuple(prepared_changes)


async def _run_tracked_review_cleanup_pass(
    *,
    github_client: GithubClient,
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int] = frozenset(),
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Clean closed exact review records while preserving open or ambiguous ones."""

    if not prepared_changes:
        return
    remote = prepared_cleanup.remote
    if remote is None:
        raise AssertionError("Tracked review cleanup requires a configured remote.")
    remote_name = remote.name
    observations = await _observe_cleanup_reviews(
        change_ids=tuple(change.candidate.change_id for change in prepared_changes),
        context=prepared_cleanup.context,
        github_client=github_client,
        remote_name=remote_name,
    )
    for prepared_change in prepared_changes:
        candidate = prepared_change.candidate
        observation = observations[candidate.change_id]
        if isinstance(observation, GithubClientError):
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect saved PR "
                    t"#{candidate.review_identity.pr_number} for "
                    t"{ui.change_id(candidate.change_id)}; fix GitHub access, then rerun "
                    t"{ui.cmd('cleanup')}",
                )
            )
            continue
        stop_after_failure = await _cleanup_tracked_review(
            github_client=github_client,
            initial_observation=observation,
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            preview_detached_dependents=preview_detached_dependents,
            record_action=record_action,
            remote_name=remote_name,
        )
        if stop_after_failure:
            break


async def _observe_cleanup_reviews(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
    github_client: GithubClient,
    remote_name: str,
) -> dict[str, RepositoryObservation | GithubClientError]:
    """Batch first, then isolate only records that GitHub cannot decode."""

    async def observe_one(change_id: str) -> RepositoryObservation | GithubClientError:
        try:
            return await observe_reviews(
                change_ids=(change_id,),
                context=context,
                github_client=github_client,
                include_open_dependents=True,
                remote_name=remote_name,
            )
        except GithubClientError as error:
            return error

    try:
        observation = await observe_reviews(
            change_ids=change_ids,
            context=context,
            github_client=github_client,
            include_open_dependents=True,
            remote_name=remote_name,
        )
    except GithubClientError:
        values = await run_bounded_tasks(
            concurrency=DEFAULT_BOUNDED_CONCURRENCY,
            items=change_ids,
            run_item=observe_one,
        )
    else:
        values = (observation,) * len(change_ids)
    return dict(zip(change_ids, values, strict=True))


async def _cleanup_tracked_review(
    *,
    github_client: GithubClient,
    initial_observation: RepositoryObservation,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int],
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
) -> bool:
    """Plan and apply one cleanup, returning whether a partial failure must stop the pass."""

    candidate = prepared_change.candidate
    identity = candidate.review_identity
    review_state, update, blocker_action = _review_cleanup_update(
        close_open_pull_requests=prepared_cleanup.close_open_pull_requests,
        observation=initial_observation,
        prepared_change=prepared_change,
        preview_detached_dependents=preview_detached_dependents,
    )
    if blocker_action is not None:
        record_action(blocker_action)
        return False
    if review_state == "open" and not prepared_cleanup.close_open_pull_requests:
        if prepared_change.stale_reason is not None:
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="skipped",
                    body=t"preserve open orphan PR #{identity.pr_number}",
                )
            )
        return False
    if review_state == "merged" and prepared_change.has_mutable_copy:
        record_action(
            CleanupAction(
                kind="tracking",
                status="skipped",
                body=t"preserve merged PR #{identity.pr_number} for "
                t"{ui.change_id(candidate.change_id)}; run "
                t"{ui.cmd(f'sync {candidate.change_id}')} before cleanup",
            )
        )
        return False
    stack_blocker = await github_stack_cleanup_blocker(
        github_client=github_client,
        pull_number=identity.pr_number,
    )
    if stack_blocker is not None:
        record_action(_cleanup_action(stack_blocker))
        return False
    overview_lookup = await _preflight_cleanup_overview_comment(
        github_client=github_client,
        identity=identity,
        record_action=record_action,
    )
    if overview_lookup is None:
        return False
    if review_state == "open":
        close_action = CleanupAction(
            kind="pull request",
            status="planned" if prepared_cleanup.dry_run else "applied",
            body=t"close PR #{identity.pr_number}",
        )
        if not prepared_cleanup.dry_run:
            try:
                await github_client.close_pull_request(pull_number=identity.pr_number)
            except GithubClientError as error:
                record_action(
                    CleanupAction(
                        kind="pull request",
                        status="blocked",
                        body=t"cannot close PR #{identity.pr_number}: {error}",
                    )
                )
                return True
        record_action(close_action)
    return await _apply_tracked_review_cleanup(
        branch_update=update,
        overview_lookup=overview_lookup,
        github_client=github_client,
        prepared_change=prepared_change,
        prepared_cleanup=prepared_cleanup,
        record_action=record_action,
        remote_name=remote_name,
    )


def _review_cleanup_update(
    *,
    close_open_pull_requests: bool,
    observation: RepositoryObservation,
    prepared_change: PreparedCleanupChange,
    preview_detached_dependents: frozenset[int] = frozenset(),
) -> tuple[str, ReviewRefUpdate | None, CleanupAction | None]:
    """Check the exact review and derive its remote branch deletion."""

    candidate = prepared_change.candidate
    pull_request, blocker = check_tracked_review(
        allowed_states=frozenset({"open", "closed", "merged"}),
        candidate=candidate,
        observation=observation,
    )
    if blocker is not None:
        return "blocked", None, _cleanup_action(blocker)
    if pull_request is None:
        raise AssertionError("Exact cleanup lookup must return a pull request.")
    if pull_request.state == "open" and not close_open_pull_requests:
        return "open", None, None
    _pull_request, update, blocker = plan_review_cleanup(
        allowed_states=(
            frozenset({"open", "closed", "merged"})
            if close_open_pull_requests
            else frozenset({"closed", "merged"})
        ),
        candidate=candidate,
        observation=observation,
        preview_detached_dependents=preview_detached_dependents,
    )
    return pull_request.state, update, None if blocker is None else _cleanup_action(blocker)


async def _preflight_cleanup_overview_comment(
    *,
    github_client: GithubClient,
    identity: ReviewIdentity,
    record_action: Callable[[CleanupAction], None],
) -> OverviewCommentLookup | None:
    """Resolve the overview comment, recording and stopping on an ambiguous lookup."""

    lookup = await find_overview_comment(
        github_client=github_client,
        pull_request_number=identity.pr_number,
    )
    if lookup.blocked_reason is not None:
        record_action(
            CleanupAction(
                kind=STACK_OVERVIEW_COMMENT_LABEL,
                status="blocked",
                body=lookup.blocked_reason,
            )
        )
        return None
    return lookup


async def _apply_tracked_review_cleanup(
    *,
    branch_update: ReviewRefUpdate | None,
    overview_lookup: OverviewCommentLookup,
    github_client: GithubClient,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
) -> bool:
    """Apply checked cleanup, returning whether a partial failure must stop the pass."""

    candidate = prepared_change.candidate
    mutation_started = not prepared_cleanup.dry_run and (
        branch_update is not None or overview_lookup.comment is not None
    )
    apply_remote_branch_cleanup(
        dry_run=prepared_cleanup.dry_run,
        jj_client=prepared_cleanup.context.jj_client,
        namespace=prepared_cleanup.context.review_namespace,
        record_action=lambda action: record_action(_cleanup_action(action)),
        remote_name=remote_name,
        update=branch_update,
    )
    comment_actions, comments_current = await apply_overview_comment_cleanup(
        dry_run=prepared_cleanup.dry_run,
        github_client=github_client,
        lookup=overview_lookup,
        pull_request_number=candidate.review_identity.pr_number,
    )
    for action in comment_actions:
        record_action(_cleanup_action(action))
    if not comments_current:
        return mutation_started
    reason = (
        f" ({prepared_change.stale_reason})" if prepared_change.stale_reason is not None else ""
    )
    action = CleanupAction(
        kind="tracking",
        status="planned" if prepared_cleanup.dry_run else "applied",
        body=t"forget PR #{candidate.review_identity.pr_number} for "
        t"{ui.change_id(candidate.change_id)}{reason}",
    )
    if prepared_cleanup.dry_run:
        record_action(action)
    else:
        prepared_cleanup.context.state_store.retire_review(
            candidate.change_id,
        )
        record_action(action)
    return False


def _cleanup_action(action: ReviewMutationAction) -> CleanupAction:
    return CleanupAction(kind=action.kind, status=action.status, body=action.body)


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.github_target is not None:
        return prepared_cleanup
    return replace(
        prepared_cleanup,
        github_target=resolve_github_target(
            prepared_cleanup.context.jj_client.list_git_remotes()
        ),
    )


def _cleanup_needs_remote_context(
    *,
    prepared_cleanup: PreparedCleanup,
) -> bool:
    """Whether plain cleanup might need remote or GitHub state beyond local checks."""

    return any(
        change_id in prepared_cleanup.state.submitted_baselines
        for change_id in (
            tuple(prepared_cleanup.state.review_identities)
            if prepared_cleanup.selected_change_ids is None
            else prepared_cleanup.selected_change_ids
        )
    )
