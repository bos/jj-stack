"""Orphan-close path for `unstack --cleanup --pull-request <pr>`."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.commands._close_actions import (
    BookmarkCleanupPlan as _OrphanBookmarkCleanupPlan,
    CloseAction,
    ManagedCommentLookup,
    apply_bookmark_cleanup,
    apply_managed_comment_cleanup,
    authorize_current_review_cleanup,
    authorize_current_tracked_pull_request,
    authorize_tracked_review,
    close_current_tracked_pull_request,
    emit_close_actions,
    find_managed_comments,
    github_observation_blocker,
    plan_bookmark_cleanup,
)
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.resolution import (
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.github.stack_comments import stack_comment_label
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
)
from jj_stack.review.bookmarks import bookmark_cleanup_allowed
from jj_stack.review.change_status import (
    classify_review_change,
)
from jj_stack.review.observation import observe_reviews
from jj_stack.ui import plain_text


@dataclass(frozen=True, slots=True)
class _OrphanCloseRun:
    """Shared execution context for one orphan close cleanup run."""

    context: CommandContext
    dry_run: bool

    @property
    def jj_client(self) -> JjClient:
        return self.context.jj_client


@dataclass(frozen=True, slots=True)
class _PreparedOrphanClose:
    """Preflighted inputs for one orphan mutation phase."""

    bookmark: str
    change_id: str
    cleanup_plan: _OrphanBookmarkCleanupPlan
    comment_lookups: tuple[ManagedCommentLookup, ...]
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
    dry_run: bool,
    pull_request_number: int,
    state: ReviewState,
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

    del dry_run, state
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
    bookmark = review_identity.head_ref

    github_target = resolve_github_target(jj_client.list_git_remotes())
    if isinstance(github_target, UnresolvedGithubTarget):
        for message in github_target_unavailable_messages(github_target):
            console.warning(message)
        return 1
    remote = github_target.remote
    github_repository = github_target.repository

    cleanup_bookmark = bookmark_cleanup_allowed(
        bookmark=bookmark,
        change_id=change_id,
    )
    if cleanup_bookmark:
        jj_client.fetch_remote(remote=remote.name, branches=(bookmark,))
    bookmark_state = jj_client.get_bookmark_state(bookmark)
    recorder = ActionRecorder[CloseAction](blocks=lambda action: action.status == "blocked")
    run = _OrphanCloseRun(
        context=context,
        dry_run=dry_run,
    )
    async with build_github_client(repository=github_repository) as github_client:
        prepared = await _preflight_orphan_close(
            bookmark=bookmark,
            bookmark_state=bookmark_state,
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
    bookmark: str,
    bookmark_state: BookmarkState,
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
            github_client=github_client,
            include_open_dependents=True,
            state_store=run.context.state_store,
        )
    except GithubClientError:
        recorder.record(github_observation_blocker())
        return None
    pull_request, blocked_action = authorize_tracked_review(
        allowed_states=frozenset({"open", "closed", "merged"}),
        change_id=change_id,
        observation=observation,
        require_no_open_dependents=True,
        retry_command=f"unstack --cleanup --pull-request {pull_request_number}",
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )
    if blocked_action is not None:
        recorder.record(blocked_action)
        return None
    if pull_request is None:
        raise AssertionError("Orphan close inspection must resolve a pull request state.")

    cleanup_plan = _preflight_orphan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        change_id=change_id,
        recorder=recorder,
        remote_name=remote_name,
        review_identity=review_identity,
        run=run,
        saved_commit_id=submitted_baseline.commit_id,
    )
    if recorder.blocked:
        return None
    comment_lookups = await _preflight_orphaned_comment_cleanup(
        github_client=github_client,
        pull_request_number=pull_request_number,
        recorder=recorder,
    )
    if recorder.blocked:
        return None
    return _PreparedOrphanClose(
        bookmark=bookmark,
        change_id=change_id,
        cleanup_plan=cleanup_plan,
        comment_lookups=comment_lookups,
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
    """Apply the preflighted orphan mutations in their authorization order."""

    pull_request = await _dissolve_orphan_native_stack(
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

    comment_actions, comments_current = await apply_managed_comment_cleanup(
        change_id=prepared.change_id,
        dry_run=run.dry_run,
        github_client=github_client,
        lookups=prepared.comment_lookups,
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


async def _dissolve_orphan_native_stack(
    *,
    github_client: GithubClient,
    prepared: _PreparedOrphanClose,
    recorder: ActionRecorder[CloseAction],
    run: _OrphanCloseRun,
) -> GithubPullRequest | None:
    """Freshly authorize and dissolve native membership when orphan cleanup needs it."""

    if run.dry_run:
        pull_request = prepared.initial_pull_request
        blocked_action = None
    else:
        pull_request, blocked_action = await authorize_current_tracked_pull_request(
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
    if pull_request.state != "open" and not prepared.cleanup_plan.remote_delete:
        return pull_request

    selection = GithubStackSelection(
        github_client,
        (prepared.review_identity.pr_number,),
        run.context.state_store,
    )
    try:
        native_stack = (
            await selection.authorize_exact_active_suffix(persist=False)
            if run.dry_run
            else await selection.dissolve_exact()
        )
    except CliError as error:
        recorder.record(CloseAction(kind="GitHub stack", body=str(error), status="blocked"))
        return None
    if native_stack is not None:
        recorder.record(
            CloseAction(
                kind="GitHub stack",
                body=t"dissolve GitHub stack #{native_stack.number}",
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
    """Delete the exact branch artifacts, then retire the authorized pair."""

    if not run.dry_run:
        blocked_action = await authorize_current_review_cleanup(
            change_id=prepared.change_id,
            delete_remote_branch=prepared.cleanup_plan.remote_delete,
            github_client=github_client,
            retry_command=(
                f"unstack --cleanup --pull-request {prepared.review_identity.pr_number}"
            ),
            review_identity=prepared.review_identity,
            state_store=run.context.state_store,
            submitted_baseline=prepared.submitted_baseline,
        )
        if blocked_action is not None:
            recorder.record(blocked_action)
            return
    cleanup_current = apply_bookmark_cleanup(
        bookmark=prepared.bookmark,
        change_id=prepared.change_id,
        cleanup_plan=prepared.cleanup_plan,
        local_commit_id=prepared.submitted_baseline.commit_id,
        record_action=recorder.record,
        remote_name=prepared.remote_name,
        remote_commit_id=prepared.submitted_baseline.commit_id,
        review_identity=prepared.review_identity,
        run=run,
    )
    if not cleanup_current:
        return

    if not run.dry_run:
        _pull_request, blocked_action = await authorize_current_tracked_pull_request(
            allowed_states=frozenset({"closed", "merged"}),
            change_id=prepared.change_id,
            github_client=github_client,
            require_no_open_dependents=True,
            retry_command=(
                f"unstack --cleanup --pull-request {prepared.review_identity.pr_number}"
            ),
            review_identity=prepared.review_identity,
            state_store=run.context.state_store,
            submitted_baseline=prepared.submitted_baseline,
        )
        if blocked_action is not None:
            recorder.record(blocked_action)
            return
    recorder.record(
        CloseAction(
            kind="tracking data",
            body=t"prune orphan record for {ui.change_id(prepared.change_id)}",
            status="planned" if run.dry_run else "applied",
        )
    )
    if not run.dry_run:
        run.context.state_store.retire_review(
            prepared.change_id,
            expected_identity=prepared.review_identity,
            expected_baseline=prepared.submitted_baseline,
        )


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


def _preflight_orphan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    change_id: str,
    recorder: ActionRecorder[CloseAction],
    remote_name: str,
    review_identity: ReviewIdentity,
    run: _OrphanCloseRun,
    saved_commit_id: str,
) -> _OrphanBookmarkCleanupPlan:
    dry_run = run.dry_run
    remote_state = bookmark_state.remote_target(remote_name)
    review_status = classify_review_change(
        commit_id=saved_commit_id,
        local="orphaned",
        pull_request_lookup=None,
        remote_state=remote_state,
        review_identity=review_identity,
    )
    if review_status.remote_branch == "absent":
        branch_label = f"{bookmark}@{remote_name}"
        recorder.record(
            CloseAction(
                kind="remote branch",
                body=t"{ui.bookmark(branch_label)} already absent",
                status="planned" if dry_run else "applied",
            )
        )
    return _plan_orphan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        change_id=change_id,
        commit_id=saved_commit_id,
        recorder=recorder,
        remote_name=remote_name,
        review_identity=review_identity,
        run=run,
    )


def _plan_orphan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    change_id: str,
    commit_id: str,
    recorder: ActionRecorder[CloseAction],
    remote_name: str,
    review_identity: ReviewIdentity,
    run: _OrphanCloseRun,
) -> _OrphanBookmarkCleanupPlan:
    return plan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        change_id=change_id,
        local_commit_id=commit_id,
        record_action=recorder.record,
        remote_name=remote_name,
        remote_commit_id=commit_id,
        review_identity=review_identity,
    )


async def _preflight_orphaned_comment_cleanup(
    *,
    github_client: GithubClient,
    pull_request_number: int,
    recorder: ActionRecorder[CloseAction],
) -> tuple[ManagedCommentLookup, ...]:
    lookups = await find_managed_comments(
        github_client=github_client,
        pull_request_number=pull_request_number,
    )
    for lookup in lookups:
        if lookup.blocked_reason is not None:
            recorder.record(
                CloseAction(
                    kind=stack_comment_label(lookup.kind),
                    body=lookup.blocked_reason,
                    status="blocked",
                )
            )
            return ()
    return lookups
