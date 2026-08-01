"""Orphan-close path for `unstack --cleanup --pull-request <pr>`."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.commands._close_actions import (
    CloseAction,
    OverviewCommentLookup,
    apply_overview_comment_cleanup,
    apply_remote_branch_cleanup,
    check_current_review_cleanup,
    check_current_tracked_pull_request,
    close_current_tracked_pull_request,
    emit_close_actions,
    find_overview_comment,
    github_observation_blocker,
    plan_review_cleanup,
)
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.commands._github_stack_safety import GithubStackSelection
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_LABEL
from jj_stack.github.resolution import (
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.jj.client import ReviewRefUpdate
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
)
from jj_stack.review.observation import observe_reviews
from jj_stack.ui import plain_text


@dataclass(frozen=True, slots=True)
class _OrphanCloseRun:
    """Shared execution context for one orphan close cleanup run."""

    context: CommandContext
    dry_run: bool


@dataclass(frozen=True, slots=True)
class _PreparedOrphanClose:
    """Preflighted inputs for one orphan mutation phase."""

    branch_update: ReviewRefUpdate | None
    change_id: str
    overview_lookup: OverviewCommentLookup
    initial_pull_request: GithubPullRequest
    remote_name: str
    review_identity: ReviewIdentity
    submitted_baseline: SubmittedBaseline


def state_has_pull_request_record(
    *,
    pull_request_number: int,
    state: ReviewState,
) -> bool:
    return any(
        review_identity.pr_number == pull_request_number
        for review_identity in state.review_identities.values()
    )


async def run_untracked_cleanup_pull_request(
    *,
    context: CommandContext,
    pull_request_number: int,
) -> int:
    """Handle cleanup by PR number after saved tracking was already retired."""

    github_target = resolve_github_target(context.jj_client.list_git_remotes())
    if isinstance(github_target, UnresolvedGithubTarget):
        if github_target.remote is not None:
            detail = f"remote {github_target.remote.name} is not a GitHub remote"
        elif github_target.remote_error is not None:
            detail = plain_text(github_target.remote_error)
        else:
            detail = "no GitHub remote is configured"
        raise _untracked_cleanup_verification_error(
            detail=detail,
            pull_request_number=pull_request_number,
        )

    async with build_github_client(repository=github_target.repository) as github_client:
        try:
            pull_request = await github_client.get_pull_request(
                pull_number=pull_request_number,
            )
        except GithubClientError as error:
            raise _untracked_cleanup_verification_error(
                detail=error.user_facing_reason(),
                pull_request_number=pull_request_number,
            ) from error

    if pull_request.state != "closed":
        raise CliError(
            t"PR #{pull_request_number} is not tracked locally, and it is still open.",
            hint=(
                t"Run {ui.cmd('checkout')} or {ui.cmd('relink')} if this PR should be "
                t"attached locally, or close it on GitHub."
            ),
        )

    console.output(t"Nothing to close for PR #{pull_request_number}.")
    return 0


def _untracked_cleanup_verification_error(
    *,
    detail: str,
    pull_request_number: int,
) -> CliError:
    return CliError(
        t"Could not verify whether PR #{pull_request_number} was already cleaned up.",
        hint=(
            t"{detail}. Restore GitHub access and retry "
            t"{ui.cmd(f'unstack --cleanup --pull-request {pull_request_number}')}."
        ),
    )


async def run_orphan_close(
    *,
    change_id: str,
    context: CommandContext,
    dry_run: bool,
    pull_request_number: int,
    state: ReviewState,
) -> int:
    """Close an orphaned PR, deleting its review artifacts via tracking data."""

    jj_client = context.jj_client
    review_identity = state.review_identities.get(change_id)
    submitted_baseline = state.submitted_baselines.get(change_id)
    if review_identity is None:
        raise CliError(t"PR #{pull_request_number} is no longer tracked locally.")
    if submitted_baseline is None:
        raise CliError(
            t"PR #{pull_request_number} has no valid last submitted commit; cannot clean up "
            t"its orphaned branch.",
            hint=t"Run {ui.cmd('relink')} to repair the saved review before retrying.",
        )

    github_target = resolve_github_target(jj_client.list_git_remotes())
    if isinstance(github_target, UnresolvedGithubTarget):
        for message in github_target_unavailable_messages(github_target):
            console.warning(message)
        return 1
    remote = github_target.remote
    github_repository = github_target.repository

    jj_client.ensure_review_fetch_isolation(
        remote=remote.name,
        dry_run=dry_run,
        on_change=report_fetch_isolation,
    )
    recorder = ActionRecorder[CloseAction](blocks=lambda action: action.status == "blocked")
    run = _OrphanCloseRun(
        context=context,
        dry_run=dry_run,
    )
    async with build_github_client(repository=github_repository) as github_client:
        prepared = await _preflight_orphan_close(
            change_id=change_id,
            github_client=github_client,
            pull_request_number=pull_request_number,
            recorder=recorder,
            remote_name=remote.name,
            review_identity=review_identity,
            run=run,
            submitted_baseline=submitted_baseline,
        )
        if prepared is not None:
            await _mutate_orphan_close(
                github_client=github_client,
                prepared=prepared,
                recorder=recorder,
                run=run,
            )

    return _render_orphan_close_actions(
        actions=recorder.as_tuple(),
        blocked=recorder.blocked,
        run=run,
    )


async def _preflight_orphan_close(
    *,
    change_id: str,
    github_client: GithubClient,
    pull_request_number: int,
    recorder: ActionRecorder[CloseAction],
    remote_name: str,
    review_identity: ReviewIdentity,
    run: _OrphanCloseRun,
    submitted_baseline: SubmittedBaseline,
) -> _PreparedOrphanClose | None:
    """Resolve exact PR, branch, and comment inputs before orphan mutation."""

    try:
        observation = await observe_reviews(
            change_ids=(change_id,),
            context=run.context,
            github_client=github_client,
            include_open_dependents=True,
            remote_name=remote_name,
        )
    except GithubClientError:
        recorder.record(github_observation_blocker())
        return None
    pull_request, branch_update, blocked_action = plan_review_cleanup(
        allowed_states=frozenset({"open", "closed", "merged"}),
        change_id=change_id,
        observation=observation,
        retry_command=f"unstack --cleanup --pull-request {pull_request_number}",
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )
    if blocked_action is not None:
        recorder.record(blocked_action)
        return None
    if pull_request is None:
        raise AssertionError("Orphan close inspection must resolve a pull request state.")

    overview_lookup = await _preflight_orphaned_overview_comment(
        github_client=github_client,
        pull_request_number=pull_request_number,
        recorder=recorder,
    )
    if recorder.blocked:
        return None
    return _PreparedOrphanClose(
        branch_update=branch_update,
        change_id=change_id,
        overview_lookup=overview_lookup,
        initial_pull_request=pull_request,
        remote_name=remote_name,
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )


async def _mutate_orphan_close(
    *,
    github_client: GithubClient,
    prepared: _PreparedOrphanClose,
    recorder: ActionRecorder[CloseAction],
    run: _OrphanCloseRun,
) -> None:
    """Apply the preflighted orphan mutations in dependency order."""

    pull_request = await _dissolve_orphan_github_stack(
        github_client=github_client,
        prepared=prepared,
        recorder=recorder,
        run=run,
    )
    if pull_request is None:
        return
    _pull_request, close_action = await close_current_tracked_pull_request(
        change_id=prepared.change_id,
        dry_run=run.dry_run,
        github_client=github_client,
        observed_pull_request=pull_request,
        review_identity=prepared.review_identity,
        state_store=run.context.state_store,
        submitted_baseline=prepared.submitted_baseline,
        target_label=t"orphaned change {ui.change_id(prepared.change_id)}",
    )
    if close_action is not None:
        recorder.record(close_action)
    if recorder.blocked:
        return

    comment_actions, comments_current = await apply_overview_comment_cleanup(
        change_id=prepared.change_id,
        dry_run=run.dry_run,
        github_client=github_client,
        lookup=prepared.overview_lookup,
        review_identity=prepared.review_identity,
        state_store=run.context.state_store,
        submitted_baseline=prepared.submitted_baseline,
    )
    for action in comment_actions:
        recorder.record(action)
    if comments_current:
        await _cleanup_orphan_artifacts(
            github_client=github_client,
            prepared=prepared,
            recorder=recorder,
            run=run,
        )


async def _dissolve_orphan_github_stack(
    *,
    github_client: GithubClient,
    prepared: _PreparedOrphanClose,
    recorder: ActionRecorder[CloseAction],
    run: _OrphanCloseRun,
) -> GithubPullRequest | None:
    """Recheck and dissolve stack membership when orphan cleanup needs it."""

    if run.dry_run:
        pull_request = prepared.initial_pull_request
        blocked_action = None
    else:
        pull_request, blocked_action = await check_current_tracked_pull_request(
            allowed_states=frozenset({"open", "closed", "merged"}),
            change_id=prepared.change_id,
            github_client=github_client,
            review_identity=prepared.review_identity,
            state_store=run.context.state_store,
            submitted_baseline=prepared.submitted_baseline,
        )
    if blocked_action is not None:
        recorder.record(blocked_action)
        return None
    if pull_request is None:
        raise AssertionError("Exact orphan mutation lookup must return a pull request.")

    selection = GithubStackSelection(
        github_client,
        (prepared.review_identity.pr_number,),
    )
    try:
        github_stack = (
            await selection.recheck_active_suffix()
            if run.dry_run
            else await selection.dissolve_exact()
        )
    except CliError as error:
        recorder.record(CloseAction(kind="GitHub stack", body=str(error), status="blocked"))
        return None
    if github_stack is not None:
        recorder.record(
            CloseAction(
                kind="GitHub stack",
                body=t"dissolve GitHub stack #{github_stack.number}",
                status="planned" if run.dry_run else "applied",
            )
        )
    return pull_request


async def _cleanup_orphan_artifacts(
    *,
    github_client: GithubClient,
    prepared: _PreparedOrphanClose,
    recorder: ActionRecorder[CloseAction],
    run: _OrphanCloseRun,
) -> None:
    """Delete the exact branch artifacts, then retire the checked pair."""

    if not run.dry_run:
        blocked_action = await check_current_review_cleanup(
            change_id=prepared.change_id,
            context=run.context,
            expected_update=prepared.branch_update,
            github_client=github_client,
            remote_name=prepared.remote_name,
            retry_command=(
                f"unstack --cleanup --pull-request {prepared.review_identity.pr_number}"
            ),
            review_identity=prepared.review_identity,
            submitted_baseline=prepared.submitted_baseline,
        )
        if blocked_action is not None:
            recorder.record(blocked_action)
            return
    apply_remote_branch_cleanup(
        dry_run=run.dry_run,
        jj_client=run.context.jj_client,
        record_action=recorder.record,
        remote_name=prepared.remote_name,
        update=prepared.branch_update,
    )
    if not run.dry_run:
        blocked_action = await check_current_review_cleanup(
            change_id=prepared.change_id,
            context=run.context,
            expected_update=None,
            github_client=github_client,
            remote_name=prepared.remote_name,
            retry_command=(
                f"unstack --cleanup --pull-request {prepared.review_identity.pr_number}"
            ),
            review_identity=prepared.review_identity,
            submitted_baseline=prepared.submitted_baseline,
        )
        if blocked_action is not None:
            recorder.record(blocked_action)
            return
    action = CloseAction(
        kind="tracking data",
        body=t"prune orphan record for {ui.change_id(prepared.change_id)}",
        status="planned" if run.dry_run else "applied",
    )
    if run.dry_run:
        recorder.record(action)
    else:
        run.context.state_store.retire_review(
            prepared.change_id,
            expected_identity=prepared.review_identity,
            expected_baseline=prepared.submitted_baseline,
        )
        recorder.record(action)


def _render_orphan_close_actions(
    *,
    actions: tuple[CloseAction, ...],
    blocked: bool,
    run: _OrphanCloseRun,
) -> int:
    emit_close_actions(
        actions=actions,
        applied=not run.dry_run,
        blocked=blocked,
    )
    return 1 if blocked else 0


async def _preflight_orphaned_overview_comment(
    *,
    github_client: GithubClient,
    pull_request_number: int,
    recorder: ActionRecorder[CloseAction],
) -> OverviewCommentLookup:
    lookup = await find_overview_comment(
        github_client=github_client,
        pull_request_number=pull_request_number,
    )
    if lookup.blocked_reason is not None:
        recorder.record(
            CloseAction(
                kind=STACK_OVERVIEW_COMMENT_LABEL,
                body=lookup.blocked_reason,
                status="blocked",
            )
        )
    return lookup
