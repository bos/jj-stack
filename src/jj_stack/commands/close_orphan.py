"""Orphan-close path for `unstack --cleanup --pull-request <pr>`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.commands._close_actions import (
    BookmarkCleanupPlan as _OrphanBookmarkCleanupPlan,
    CloseAction,
    apply_bookmark_cleanup,
    emit_close_actions,
    find_managed_comments,
    plan_bookmark_cleanup,
    retire_review_identity,
)
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.resolution import (
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.github.stack_comments import (
    StackCommentKind,
    delete_stack_comment,
    stack_comment_label,
)
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.github import GithubIssueComment, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.review.bookmarks import bookmark_cleanup_allowed, find_changes_by_bookmark
from jj_stack.review.change_status import (
    classify_review_change,
)
from jj_stack.ui import Message, plain_text

OrphanedPullRequestState = Literal["closed", "open"]


@dataclass(frozen=True, slots=True)
class _OrphanedPullRequestInspection:
    """Resolved GitHub view of one orphaned tracked pull request."""

    pull_request: GithubPullRequest
    state: OrphanedPullRequestState


@dataclass(frozen=True, slots=True)
class _ResolvedOrphanedComment:
    """One managed stack comment proven safe to delete during orphan cleanup."""

    comment: GithubIssueComment
    kind: StackCommentKind


@dataclass(frozen=True, slots=True)
class _OrphanCloseRun:
    """Shared execution context for one orphan close cleanup run."""

    context: CommandContext
    dry_run: bool

    @property
    def jj_client(self) -> JjClient:
        return self.context.jj_client


def state_has_pull_request_record(
    *,
    pull_request_number: int,
    state: ReviewState,
) -> bool:
    return any(
        review_identity.is_tracked and review_identity.pr_number == pull_request_number
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

    config = context.config
    jj_client = context.jj_client
    state_store = context.state_store
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
    other_claimants = tuple(
        other_change_id
        for other_change_id in find_changes_by_bookmark(state.review_identities, bookmark)
        if other_change_id != change_id
    )
    if other_claimants:
        rendered_others = ", ".join(other[:8] for other in other_claimants)
        raise CliError(
            t"Bookmark {ui.bookmark(bookmark)} is now claimed by another tracked change "
            t"({rendered_others}); refusing to delete the branch from under a live review.",
            hint=t"Run {ui.cmd('unlink')} on the orphan record instead.",
        )

    github_target = resolve_github_target(jj_client.list_git_remotes())
    if isinstance(github_target, UnresolvedGithubTarget):
        for message in github_target_unavailable_messages(github_target):
            console.warning(message)
        return 1
    remote = github_target.remote
    github_repository = github_target.repository

    label = ui.change_id(change_id)
    revision_label = t"orphaned change {label}"
    last_target = submitted_baseline.commit_id
    cleanup_bookmark = bookmark_cleanup_allowed(
        bookmark=bookmark,
        bookmark_managed=review_identity.manages_bookmark,
        cleanup_user_bookmarks=config.cleanup_user_bookmarks,
        prefix=config.bookmark_prefix,
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
        inspection, blocked_action = await _lookup_orphaned_pull_request(
            github_client=github_client,
            pull_request_number=pull_request_number,
            review_identity=review_identity,
        )
        if blocked_action is not None:
            recorder.record(blocked_action)

        cleanup_plan = _OrphanBookmarkCleanupPlan(
            local_forget=False,
            remote_delete=False,
        )
        resolved_comments: tuple[_ResolvedOrphanedComment, ...] = ()
        if not recorder.blocked and cleanup_bookmark:
            cleanup_plan = _preflight_orphan_bookmark_cleanup(
                bookmark=bookmark,
                bookmark_state=bookmark_state,
                recorder=recorder,
                remote_name=remote.name,
                review_identity=review_identity,
                run=run,
                saved_commit_id=last_target,
            )
        if not recorder.blocked:
            resolved_comments = await _preflight_orphaned_comment_cleanup(
                github_client=github_client,
                pull_request_number=pull_request_number,
                recorder=recorder,
            )
        if recorder.blocked:
            _retire_blocked_orphan_close_tracking(
                change_id=change_id,
                inspection=inspection,
                recorder=recorder,
                review_identity=review_identity,
                revision_label=revision_label,
                run=run,
            )
            return _render_orphan_close_actions(
                actions=recorder.as_tuple(),
                blocked=True,
                run=run,
            )

        if inspection is None:
            raise AssertionError("Orphan close inspection must resolve a pull request state.")
        if not dry_run and (inspection.state == "open" or cleanup_plan.remote_delete):
            try:
                native_stack = await GithubStackSelection(
                    github_client,
                    (pull_request_number,),
                    state_store,
                ).dissolve_exact()
            except CliError as error:
                recorder.record(
                    CloseAction(kind="GitHub stack", body=str(error), status="blocked")
                )
                return _render_orphan_close_actions(
                    actions=recorder.as_tuple(),
                    blocked=True,
                    run=run,
                )
            if native_stack is not None:
                recorder.record(
                    CloseAction(
                        kind="GitHub stack",
                        body=t"dissolve GitHub stack #{native_stack.number}",
                        status="applied",
                    )
                )
        if inspection.state == "open":
            recorder.record(
                CloseAction(
                    kind="pull request",
                    body=t"close PR #{pull_request_number} for orphaned change {label}",
                    status="planned" if dry_run else "applied",
                )
            )
            if not dry_run:
                try:
                    await github_client.close_pull_request(
                        pull_number=pull_request_number,
                    )
                except GithubClientError as error:
                    raise CliError(t"Could not close PR #{pull_request_number}.") from error

        await _apply_orphaned_comment_cleanup(
            github_client=github_client,
            pull_request_number=pull_request_number,
            recorder=recorder,
            resolved_comments=resolved_comments,
            run=run,
        )
        if cleanup_bookmark:
            apply_bookmark_cleanup(
                bookmark=bookmark,
                cleanup_plan=cleanup_plan,
                commit_id=last_target,
                record_action=recorder.record,
                remote_name=remote.name,
                run=run,
            )

    recorder.record(
        CloseAction(
            kind="tracking data",
            body=t"prune orphan record for {label}",
            status="planned" if dry_run else "applied",
        )
    )
    if not dry_run:
        state_store.retire_review(
            change_id,
            expected_identity=review_identity,
            expected_baseline=submitted_baseline,
        )

    return _render_orphan_close_actions(
        actions=recorder.as_tuple(),
        blocked=recorder.blocked,
        run=run,
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


def _retire_blocked_orphan_close_tracking(
    *,
    change_id: str,
    inspection: _OrphanedPullRequestInspection | None,
    recorder: ActionRecorder[CloseAction],
    review_identity: ReviewIdentity,
    revision_label: Message,
    run: _OrphanCloseRun,
) -> None:
    if inspection is None or inspection.state != "closed":
        return

    updated_identity = retire_review_identity(review_identity)
    if updated_identity == review_identity:
        return

    dry_run = run.dry_run
    recorder.record(
        CloseAction(
            kind="tracking",
            body=t"mark {revision_label} as already {inspection.pull_request.state} on GitHub",
            status="planned" if dry_run else "applied",
        )
    )
    if not dry_run:
        run.context.state_store.set_link_state(
            change_id,
            expected_identity=review_identity,
            link_state="unlinked",
        )


def _preflight_orphan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
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
    commit_id: str,
    recorder: ActionRecorder[CloseAction],
    remote_name: str,
    review_identity: ReviewIdentity,
    run: _OrphanCloseRun,
) -> _OrphanBookmarkCleanupPlan:
    config = run.context.config
    return plan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        cleanup_user_bookmarks=config.cleanup_user_bookmarks,
        commit_id=commit_id,
        prefix=config.bookmark_prefix,
        record_action=recorder.record,
        remote_name=remote_name,
        review_identity=review_identity,
    )


async def _preflight_orphaned_comment_cleanup(
    *,
    github_client: GithubClient,
    pull_request_number: int,
    recorder: ActionRecorder[CloseAction],
) -> tuple[_ResolvedOrphanedComment, ...]:
    lookups = await find_managed_comments(
        github_client=github_client,
        pull_request_number=pull_request_number,
    )
    resolved_comments: list[_ResolvedOrphanedComment] = []
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
        if lookup.comment is not None:
            resolved_comments.append(
                _ResolvedOrphanedComment(comment=lookup.comment, kind=lookup.kind)
            )
    return tuple(resolved_comments)


async def _apply_orphaned_comment_cleanup(
    *,
    github_client: GithubClient,
    pull_request_number: int,
    recorder: ActionRecorder[CloseAction],
    resolved_comments: tuple[_ResolvedOrphanedComment, ...],
    run: _OrphanCloseRun,
) -> None:
    dry_run = run.dry_run
    for resolved in resolved_comments:
        recorder.record(
            CloseAction(
                kind=stack_comment_label(resolved.kind),
                body=(
                    f"delete {stack_comment_label(resolved.kind)} #{resolved.comment.id} from "
                    f"PR #{pull_request_number}"
                ),
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            await delete_stack_comment(
                comment_id=resolved.comment.id,
                github_client=github_client,
                kind=resolved.kind,
            )


async def _lookup_orphaned_pull_request(
    *,
    github_client: GithubClient,
    pull_request_number: int,
    review_identity: ReviewIdentity,
) -> tuple[_OrphanedPullRequestInspection | None, CloseAction | None]:
    """Verify the saved PR identity and look for live duplicate branch claims."""

    bookmark = review_identity.head_ref

    try:
        pull_request = await github_client.get_pull_request(
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return (
                None,
                CloseAction(
                    kind="close",
                    body=t"PR #{pull_request_number} is no longer on GitHub",
                    status="blocked",
                ),
            )
        return None, _blocked_orphaned_close_github_action()
    inspection = _inspect_orphaned_pull_request_state(pull_request)
    if pull_request.head.ref != bookmark:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because it no longer "
                    t"has saved bookmark {ui.bookmark(bookmark)} as its head ref"
                ),
                status="blocked",
            ),
        )
    expected_head_label = f"{review_identity.head_owner}:{bookmark}"
    if pull_request.head.label != expected_head_label:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because its head "
                    t"is {pull_request.head.label or '<unknown>'}, not "
                    t"{expected_head_label}"
                ),
                status="blocked",
            ),
        )

    try:
        branch_matches = await github_client.get_pull_requests_by_head_refs(
            head_refs=(bookmark,),
        )
    except GithubClientError:
        return None, _blocked_orphaned_close_github_action()

    other_live_matches = tuple(
        candidate
        for candidate in branch_matches.get(bookmark, ())
        if candidate.number != pull_request_number
    )
    if other_live_matches:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because saved bookmark "
                    t"{ui.bookmark(bookmark)} now has multiple pull requests"
                ),
                status="blocked",
            ),
        )
    return inspection, None


def _inspect_orphaned_pull_request_state(
    pull_request: GithubPullRequest,
) -> _OrphanedPullRequestInspection:
    if pull_request.state != "closed" or pull_request.merged_at is None:
        normalized_pull_request = pull_request
    else:
        normalized_pull_request = pull_request.model_copy(update={"state": "merged"})
    state: OrphanedPullRequestState = (
        "open" if normalized_pull_request.state == "open" else "closed"
    )
    return _OrphanedPullRequestInspection(
        pull_request=normalized_pull_request,
        state=state,
    )


def _blocked_orphaned_close_github_action() -> CloseAction:
    return CloseAction(
        kind="close",
        body=(
            "cannot close pull requests tracked by jj-stack without live GitHub state; "
            "fix GitHub access and retry"
        ),
        status="blocked",
    )
