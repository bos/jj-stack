"""Remove review branches, comments, and tracking that no active review still needs.

It checks the whole repository, and only acts on reviews GitHub reports as closed or merged.

Open orphaned PRs are left alone. Run `jj-stack list` to see them, then close and clean up one
with `jj-stack unstack --cleanup --pull-request <pr>`, or clean up all of them with
`jj-stack unstack --cleanup --pull-request orphans`.

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
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.commands._close_actions import (
    CloseAction,
    ManagedCommentLookup,
    apply_managed_comment_cleanup,
    apply_remote_branch_cleanup,
    check_current_review_cleanup,
    check_tracked_review,
    find_managed_comments,
    native_stack_cleanup_blocker,
    plan_review_cleanup,
)
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.resolution import (
    GithubTarget,
    resolve_github_target,
)
from jj_stack.github.stack_comments import STACK_OVERVIEW_COMMENT_LABEL
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import ReviewRefUpdate
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_reviews,
)
from jj_stack.state.operation_lock import (
    acquire_operation_lock,
)
from jj_stack.ui import plain_text

from .shared import (
    CleanupAction,
    CleanupResult,
    PreparedCleanup,
    PreparedCleanupChange,
    _build_action_streamer,
    _emit_output_lines,
    _render_cleanup_action_header,
    _render_cleanup_postamble,
)
from .stale import LocalCleanupObservation, _local_cleanup_observations

HELP = "Remove review artifacts that are no longer in use"


def cleanup(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

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
            context=context,
            dry_run=dry_run,
        )


def _run_cleanup_command(
    *,
    context: CommandContext,
    dry_run: bool,
) -> int:
    """Render and run the stale cleanup command path."""

    with console.spinner(description="Loading review state"):
        prepared_cleanup = _prepare_cleanup(
            context=context,
            dry_run=dry_run,
        )
    local_observations = _local_cleanup_observations(
        change_ids=tuple(prepared_cleanup.state.review_identities),
        context=prepared_cleanup.context,
    )
    if _cleanup_needs_remote_context(prepared_cleanup=prepared_cleanup):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        for message in github_target_unavailable_messages(prepared_cleanup.github_target):
            console.warning(plain_text(message))

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                header=_render_cleanup_action_header(dry_run=prepared_cleanup.dry_run),
            ),
            prepared_cleanup=prepared_cleanup,
            local_observations=local_observations,
        )
    )
    _emit_output_lines(_render_cleanup_postamble(result=result))
    return 1 if any(action.status == "blocked" for action in result.actions) else 0


def _prepare_cleanup(
    *,
    context: CommandContext,
    dry_run: bool,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    state_store = context.state_store
    state = state_store.load()
    if not dry_run:
        state_store.require_writable()

    return PreparedCleanup(
        context=context,
        github_target=None,
        dry_run=dry_run,
        state=state,
    )


async def _run_cleanup_async(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    local_observations: dict[str, LocalCleanupObservation],
) -> CleanupResult:
    recorder = ActionRecorder[CleanupAction](on_action=on_action)
    prepared_changes = _run_local_cleanup_pass(
        prepared_cleanup=prepared_cleanup,
        record_action=recorder.record,
        local_observations=local_observations,
    )
    github_target = prepared_cleanup.github_target
    if isinstance(github_target, GithubTarget) and prepared_changes:
        prepared_cleanup.context.jj_client.ensure_review_fetch_isolation(
            remote=github_target.remote.name,
            dry_run=prepared_cleanup.dry_run,
            on_change=report_fetch_isolation,
        )
        async with build_github_client(repository=github_target.repository) as github_client:
            await _run_tracked_review_cleanup_pass(
                github_client=github_client,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                record_action=recorder.record,
            )
    elif prepared_changes:
        for prepared_change in prepared_changes:
            recorder.record(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect PR #{prepared_change.review_identity.pr_number} for "
                    t"{ui.change_id(prepared_change.change_id)} because the GitHub repository "
                    t"cannot be resolved",
                )
            )
    return CleanupResult(actions=recorder.as_tuple())


def _run_local_cleanup_pass(
    *,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    local_observations: dict[str, LocalCleanupObservation],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    for change_id, review_identity in prepared_cleanup.state.review_identities.items():
        submitted_baseline = prepared_cleanup.state.submitted_baselines.get(change_id)
        if submitted_baseline is None:
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="skipped",
                    body=t"leave {ui.change_id(change_id)} tracked because its last submitted "
                    t"commit is unavailable; run {ui.cmd('relink')} to repair PR tracking",
                )
            )
            continue
        local_observation = local_observations.get(
            change_id,
            LocalCleanupObservation(
                stale_reason="local change was not inspected",
            ),
        )
        prepared_change = PreparedCleanupChange(
            change_id=change_id,
            review_identity=review_identity,
            stale_reason=local_observation.stale_reason,
            submitted_baseline=submitted_baseline,
        )
        prepared_changes.append(prepared_change)
    return tuple(prepared_changes)


async def _run_tracked_review_cleanup_pass(
    *,
    github_client: GithubClient,
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
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
        change_ids=tuple(change.change_id for change in prepared_changes),
        context=prepared_cleanup.context,
        github_client=github_client,
        remote_name=remote_name,
    )
    for prepared_change in prepared_changes:
        observation = observations[prepared_change.change_id]
        if isinstance(observation, GithubClientError):
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect saved PR "
                    t"#{prepared_change.review_identity.pr_number} for "
                    t"{ui.change_id(prepared_change.change_id)}; fix GitHub access, then rerun "
                    t"{ui.cmd('cleanup')}",
                )
            )
            continue
        await _cleanup_tracked_review(
            github_client=github_client,
            initial_observation=observation,
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
            remote_name=remote_name,
        )


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
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
) -> None:
    """Plan and apply cleanup for one exact closed review."""

    identity = prepared_change.review_identity
    open_review, update, blocker_action = _review_cleanup_update(
        observation=initial_observation,
        prepared_change=prepared_change,
    )
    if blocker_action is not None:
        record_action(blocker_action)
        return
    if open_review:
        if prepared_change.stale_reason is not None:
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="skipped",
                    body=t"preserve open orphan PR #{identity.pr_number}",
                )
            )
        return
    native_blocker = await native_stack_cleanup_blocker(
        github_client=github_client,
        pull_number=identity.pr_number,
    )
    if native_blocker is not None:
        record_action(_cleanup_action(native_blocker))
        return
    comment_lookups = await _preflight_cleanup_comments(
        github_client=github_client,
        identity=identity,
        record_action=record_action,
    )
    if comment_lookups is None:
        return
    if not prepared_cleanup.dry_run:
        mutation_blocker = await check_current_review_cleanup(
            change_id=prepared_change.change_id,
            context=prepared_cleanup.context,
            expected_update=update,
            github_client=github_client,
            remote_name=remote_name,
            review_identity=prepared_change.review_identity,
            submitted_baseline=prepared_change.submitted_baseline,
        )
        if mutation_blocker is not None:
            record_action(_cleanup_action(mutation_blocker))
            return
    await _apply_tracked_review_cleanup(
        branch_update=update,
        comment_lookups=comment_lookups,
        github_client=github_client,
        prepared_change=prepared_change,
        prepared_cleanup=prepared_cleanup,
        record_action=record_action,
        remote_name=remote_name,
    )


def _review_cleanup_update(
    *,
    observation: RepositoryObservation,
    prepared_change: PreparedCleanupChange,
) -> tuple[bool, ReviewRefUpdate | None, CleanupAction | None]:
    """Check the exact review and derive its remote branch deletion."""

    identity = prepared_change.review_identity
    pull_request, blocker = check_tracked_review(
        allowed_states=frozenset({"open", "closed", "merged"}),
        change_id=prepared_change.change_id,
        observation=observation,
        review_identity=identity,
        submitted_baseline=prepared_change.submitted_baseline,
    )
    if blocker is not None:
        return False, None, _cleanup_action(blocker)
    if pull_request is None:
        raise AssertionError("Exact cleanup lookup must return a pull request.")
    if pull_request.state == "open":
        return True, None, None
    _pull_request, update, blocker = plan_review_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        change_id=prepared_change.change_id,
        observation=observation,
        review_identity=identity,
        submitted_baseline=prepared_change.submitted_baseline,
    )
    return False, update, None if blocker is None else _cleanup_action(blocker)


async def _preflight_cleanup_comments(
    *,
    github_client: GithubClient,
    identity: ReviewIdentity,
    record_action: Callable[[CleanupAction], None],
) -> tuple[ManagedCommentLookup, ...] | None:
    """Resolve managed comments, recording and stopping on ambiguous lookup."""

    lookups = await find_managed_comments(
        github_client=github_client,
        pull_request_number=identity.pr_number,
    )
    for lookup in lookups:
        if lookup.blocked_reason is None:
            continue
        record_action(
            CleanupAction(
                kind=STACK_OVERVIEW_COMMENT_LABEL,
                status="blocked",
                body=lookup.blocked_reason,
            )
        )
        return None
    return lookups


async def _apply_tracked_review_cleanup(
    *,
    branch_update: ReviewRefUpdate | None,
    comment_lookups: tuple[ManagedCommentLookup, ...],
    github_client: GithubClient,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
) -> None:
    """Apply a checked branch/comment cleanup and retire the exact pair."""

    identity = prepared_change.review_identity
    baseline = prepared_change.submitted_baseline
    apply_remote_branch_cleanup(
        dry_run=prepared_cleanup.dry_run,
        jj_client=prepared_cleanup.context.jj_client,
        record_action=lambda action: record_action(_cleanup_action(action)),
        remote_name=remote_name,
        update=branch_update,
    )
    comment_actions, comments_current = await apply_managed_comment_cleanup(
        change_id=prepared_change.change_id,
        dry_run=prepared_cleanup.dry_run,
        github_client=github_client,
        lookups=comment_lookups,
        review_identity=identity,
        state_store=prepared_cleanup.context.state_store,
        submitted_baseline=baseline,
    )
    for action in comment_actions:
        record_action(_cleanup_action(action))
    if not comments_current:
        return
    if not prepared_cleanup.dry_run:
        blocker = await check_current_review_cleanup(
            change_id=prepared_change.change_id,
            context=prepared_cleanup.context,
            expected_update=None,
            github_client=github_client,
            remote_name=remote_name,
            review_identity=identity,
            submitted_baseline=baseline,
        )
        if blocker is not None:
            record_action(_cleanup_action(blocker))
            return
    reason = (
        f" ({prepared_change.stale_reason})" if prepared_change.stale_reason is not None else ""
    )
    action = CleanupAction(
        kind="tracking",
        status="planned" if prepared_cleanup.dry_run else "applied",
        body=t"remove tracking for {ui.change_id(prepared_change.change_id)}{reason}",
    )
    if prepared_cleanup.dry_run:
        record_action(action)
    else:
        prepared_cleanup.context.state_store.retire_review(
            prepared_change.change_id,
            expected_identity=identity,
            expected_baseline=baseline,
        )
        record_action(action)


def _cleanup_action(action: CloseAction) -> CleanupAction:
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
        for change_id in prepared_cleanup.state.review_identities
    )
