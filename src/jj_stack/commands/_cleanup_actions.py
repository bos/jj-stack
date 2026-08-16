"""Shared review checks and cleanup helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_LABEL,
    delete_stack_overview_comment,
    is_overview_comment,
)
from jj_stack.jj.client import JjClient, ReviewRefUpdate
from jj_stack.models.github import GithubIssueComment, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline
from jj_stack.review.github_stack_safety import GithubStackSelection
from jj_stack.review.observation import RepositoryObservation
from jj_stack.review_namespace import ReviewNamespace, review_branch_matches_change
from jj_stack.ui import Message

ActionPresentationStatus = Literal["applied", "blocked", "planned", "skipped"]
ReviewMutationActionStatus = Literal["applied", "blocked", "planned"]
type ReviewMutationActionBody = Message


@dataclass(frozen=True, slots=True)
class ReviewMutationAction:
    """One review mutation that was planned, applied, or blocked."""

    kind: str
    status: ReviewMutationActionStatus
    body: ReviewMutationActionBody


def check_tracked_review(
    *,
    allowed_states: frozenset[str],
    change_id: str,
    observation: RepositoryObservation,
    preview_detached_dependents: frozenset[int] = frozenset(),
    require_no_open_dependents: bool = False,
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    submitted_baseline: SubmittedBaseline,
) -> tuple[GithubPullRequest | None, ReviewMutationAction | None]:
    """Check one exact unchanged review against shared observations."""

    observed = observation.reviews[change_id]
    pull_request_number = review_identity.pr_number
    pull_request = observed.pull_request
    kind = "pull request"
    reason: Message | None = None
    if (observed.identity, observed.baseline) != (review_identity, submitted_baseline):
        kind = "tracking"
        reason = (
            t"tracking for {ui.change_id(change_id)} changed while this command ran; "
            t"rerun the same command"
        )
    elif review_identity.repository_key != observation.repository.repository_key:
        reason = (
            t"cannot inspect saved PR #{pull_request_number} because it belongs to a "
            t"different GitHub repository; point the remote back at it, or reattach the "
            t"change with {ui.cmd('jj-stack relink')}"
        )
    elif pull_request is None:
        reason = (
            t"PR #{pull_request_number} is no longer on GitHub; attach a replacement with "
            t"{ui.cmd('jj-stack relink')}, or drop the tracking with "
            t"{ui.cmd('jj-stack unstack --local')}"
        )
    else:
        pull_request = pull_request.normalize_state()
    if reason is None:
        assert pull_request is not None
        exact_link = (
            review_identity.matches_pull_request(pull_request),
            tuple(pr.number for pr in observed.head_pull_requests),
        )
        if exact_link != (True, (pull_request_number,)):
            reason = (
                t"cannot inspect saved PR #{pull_request_number} because its live PR and head "
                t"no longer uniquely match {ui.bookmark(review_identity.head_ref)}"
            )
        elif pull_request.head.sha != submitted_baseline.commit_id:
            reason = (
                t"cannot mutate saved PR #{pull_request_number} because its head no longer "
                t"matches the saved submitted commit"
            )
        elif pull_request.state not in allowed_states:
            reason = (
                t"cannot mutate saved PR #{pull_request_number} because GitHub now reports "
                t"state {pull_request.state!r}"
            )
    check_dependents = (reason is None, require_no_open_dependents) == (True, True)
    if check_dependents:
        open_pull_requests_by_base = observation.open_pull_requests_by_base
        assert open_pull_requests_by_base is not None
        observed_dependents = open_pull_requests_by_base.get(review_identity.head_ref, ())
        dependents = tuple(
            filter(
                lambda item: item.number not in preview_detached_dependents,
                observed_dependents,
            )
        )
        # A full 100-result page may hide another dependent, so it also fails closed.
        blockers = dependents[:1] or observed_dependents[99:100]
        if blockers:
            kind = "remote branch"
            dependent = blockers[0]
            reason = (
                t"preserve PR #{pull_request_number}'s branch and tracking because open "
                t"PR #{dependent.number} still uses {ui.bookmark(review_identity.head_ref)} "
                t"as its base; close or retarget PR #{dependent.number}, then rerun "
                t"{ui.cmd(retry_command)}"
            )
    return (
        pull_request,
        None
        if reason is None
        else ReviewMutationAction(kind=kind, body=reason, status="blocked"),
    )


@dataclass(frozen=True, slots=True)
class OverviewCommentLookup:
    """Resolution of the managed overview comment on one pull request.

    At most one of ``comment`` or ``blocked_reason`` is set. Both are ``None`` when the marker is
    absent.
    """

    comment: GithubIssueComment | None = None
    blocked_reason: str | None = None


async def find_overview_comment(
    *,
    github_client: GithubClient,
    pull_request_number: int,
) -> OverviewCommentLookup:
    """Discover the managed overview comment for one PR via a single list call."""

    try:
        comments = await github_client.list_issue_comments(
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return OverviewCommentLookup()
        reason = error.user_facing_reason()
        return OverviewCommentLookup(
            blocked_reason=(
                f"cannot inspect the {STACK_OVERVIEW_COMMENT_LABEL} for PR "
                f"#{pull_request_number}: {reason}"
            ),
        )

    return _resolve_overview_comment_from_listed(
        comments=comments,
        pull_request_number=pull_request_number,
    )


async def apply_overview_comment_cleanup(
    *,
    dry_run: bool,
    github_client: GithubClient,
    lookup: OverviewCommentLookup,
    pull_request_number: int,
) -> tuple[tuple[ReviewMutationAction, ...], bool]:
    """Delete one overview comment identified during cleanup planning."""

    comment = lookup.comment
    if comment is None:
        return (), True
    deleted = True
    if not dry_run:
        try:
            deleted = await delete_stack_overview_comment(
                comment_id=comment.id,
                github_client=github_client,
            )
        except CliError as error:
            return (
                ReviewMutationAction(
                    kind=STACK_OVERVIEW_COMMENT_LABEL,
                    body=str(error),
                    status="blocked",
                ),
            ), False
    action_body = (
        f"delete {STACK_OVERVIEW_COMMENT_LABEL} #{comment.id} from PR #{pull_request_number}"
    )
    if not dry_run and not deleted:
        action_body = (
            f"{STACK_OVERVIEW_COMMENT_LABEL} #{comment.id} already absent from "
            f"PR #{pull_request_number}"
        )
    return (
        ReviewMutationAction(
            kind=STACK_OVERVIEW_COMMENT_LABEL,
            body=action_body,
            status="planned" if dry_run else "applied",
        ),
    ), True


def _resolve_overview_comment_from_listed(
    *,
    comments: tuple[GithubIssueComment, ...],
    pull_request_number: int,
) -> OverviewCommentLookup:
    matching_comments = [comment for comment in comments if is_overview_comment(comment.body)]
    if len(matching_comments) > 1:
        return OverviewCommentLookup(
            blocked_reason=(
                f"cannot delete {STACK_OVERVIEW_COMMENT_LABEL}s because GitHub reports "
                f"multiple candidates on PR #{pull_request_number}"
            ),
        )
    if not matching_comments:
        return OverviewCommentLookup()
    return OverviewCommentLookup(comment=matching_comments[0])


def emit_action_row(
    *,
    kind: str,
    status: ActionPresentationStatus,
    body: Message,
) -> None:
    prefix, prefix_style, body_style = _action_presentation(status)
    message = body
    if kind != "tracking":
        message = (ui.semantic_text(kind, "prefix"), ": ", body)
    console.output(
        ui.prefixed_line(
            f"{prefix} ",
            message,
            prefix_labels=prefix_style,
            message_labels=body_style,
        )
    )


def _action_presentation(
    status: ActionPresentationStatus,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    if status == "applied":
        return (
            "  ✓",
            ("signature status good",),
            None,
        )
    if status == "planned":
        return (
            "  ~",
            ("hint heading",),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ("error heading",),
            ("warning heading",),
        )
    if status == "skipped":
        return (
            "  -",
            ("hint heading",),
            None,
        )
    return ("  ?", None, None)


def plan_review_cleanup(
    *,
    allowed_states: frozenset[str],
    change_id: str,
    observation: RepositoryObservation,
    preview_detached_dependents: frozenset[int] = frozenset(),
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    submitted_baseline: SubmittedBaseline,
) -> tuple[GithubPullRequest | None, ReviewRefUpdate | None, ReviewMutationAction | None]:
    """Check exact cleanup facts and derive at most one leased ref deletion."""

    pull_request, blocker = check_tracked_review(
        allowed_states=allowed_states,
        change_id=change_id,
        observation=observation,
        preview_detached_dependents=preview_detached_dependents,
        require_no_open_dependents=True,
        retry_command=retry_command,
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )
    if blocker is not None or pull_request is None:
        return pull_request, None, blocker
    configured_repository = observation.configured_repository
    if (
        observation.remote is None
        or configured_repository is None
        or configured_repository.repository_key != review_identity.repository_key
    ):
        return (
            pull_request,
            None,
            ReviewMutationAction(
                kind="remote branch",
                body=t"cannot resolve the configured remote for saved PR "
                t"#{review_identity.pr_number}",
                status="blocked",
            ),
        )
    branch = review_identity.head_ref
    if not review_branch_matches_change(branch, change_id):
        return (
            pull_request,
            None,
            ReviewMutationAction(
                kind="tracking",
                body=t"cannot clean up {ui.bookmark(branch)} because it does not match "
                t"change {ui.change_id(change_id)}",
                status="blocked",
            ),
        )
    remote_target = observation.reviews[change_id].remote_review_target
    if remote_target is not None and remote_target != submitted_baseline.commit_id:
        return (
            pull_request,
            None,
            ReviewMutationAction(
                kind="remote branch",
                body=t"cannot delete {ui.bookmark(branch)} because it "
                t"already points to a different revision",
                status="blocked",
            ),
        )
    update = (
        None
        if remote_target is None
        else ReviewRefUpdate(
            branch=branch,
            expected_target=submitted_baseline.commit_id,
            desired_target=None,
        )
    )
    return pull_request, update, None


async def github_stack_cleanup_blocker(
    *,
    github_client: GithubClient,
    pull_number: int,
) -> ReviewMutationAction | None:
    """Fail closed when current stack membership still needs a review branch."""

    try:
        await GithubStackSelection(github_client, (pull_number,)).require_unstacked()
    except CliError as error:
        return ReviewMutationAction(kind="remote branch", body=str(error), status="blocked")
    return None


def apply_remote_branch_cleanup(
    *,
    dry_run: bool,
    jj_client: JjClient,
    namespace: ReviewNamespace,
    record_action: Callable[[ReviewMutationAction], None],
    remote_name: str,
    update: ReviewRefUpdate | None,
) -> None:
    """Execute one prechecked remote branch deletion with an exact lease.

    A rejected lease raises, so there is no failure for callers to branch on.
    """

    if update is not None:
        if not dry_run:
            jj_client.mutate_remote_review_refs(
                namespace=namespace,
                remote=remote_name,
                updates=(update,),
            )
        record_action(
            ReviewMutationAction(
                kind="remote branch",
                body=t"delete {ui.bookmark(f'{update.branch}@{remote_name}')}",
                status="planned" if dry_run else "applied",
            )
        )
