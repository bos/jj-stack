"""Stack-comment cleanup planning and execution for the plain cleanup pass."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import jj_stack.console as console
from jj_stack.commands._close_actions import find_managed_comments as _find_managed_comments
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import (
    StackCommentKind,
    stack_comment_label,
)
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.review.change_status import ReviewChangeStatus

from .shared import CleanupAction, PreparedCleanup, PreparedCleanupChange

_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY

type StackCommentCleanupEligibility = Literal["inspect", "needs-remote-check", "skip"]


@dataclass(frozen=True, slots=True)
class StackCommentCleanupPlan:
    """Planned or blocked stack-comment cleanup details."""

    actions: tuple[CleanupAction, ...]
    comments: tuple[tuple[int, StackCommentKind], ...] = ()


async def _run_stack_comment_cleanup_pass(
    *,
    github_client: GithubClient,
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    stack_comment_changes = tuple(
        prepared_change
        for prepared_change in prepared_changes
        if prepared_change.inspect_stack_comment
    )
    with console.progress(
        description="Inspecting stack comments",
        total=len(stack_comment_changes),
    ) as progress:
        comment_plans = await run_bounded_tasks(
            concurrency=_GITHUB_INSPECTION_CONCURRENCY,
            items=stack_comment_changes,
            run_item=lambda prepared_change: _plan_stack_comment_cleanup(
                github_client=github_client,
                review_identity=prepared_change.review_identity,
            ),
            on_success=lambda _index, _result: progress.advance(),
        )
    for comment_plan in comment_plans:
        if comment_plan is None:
            continue
        await _apply_stack_comment_cleanup_action(
            comment_plan=comment_plan,
            github_client=github_client,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )


async def _apply_stack_comment_cleanup_action(
    *,
    comment_plan: StackCommentCleanupPlan,
    github_client: GithubClient,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    targeted_actions = comment_plan.actions[: len(comment_plan.comments)]
    for action, (comment_id, kind) in zip(
        targeted_actions,
        comment_plan.comments,
        strict=True,
    ):
        comment_action = action
        if not prepared_cleanup.dry_run and comment_action.status == "planned":
            try:
                await github_client.delete_issue_comment(
                    comment_id=comment_id,
                )
            except GithubClientError as error:
                raise CliError(
                    f"Could not delete {stack_comment_label(kind)} #{comment_id}"
                ) from error
            comment_action = replace(action, status="applied")
        record_action(comment_action)
    for action in comment_plan.actions[len(targeted_actions) :]:
        record_action(action)


async def _plan_stack_comment_cleanup(
    *,
    github_client: GithubClient,
    review_identity: ReviewIdentity,
) -> StackCommentCleanupPlan | None:
    pull_request_number = review_identity.pr_number

    try:
        pull_request = await github_client.get_pull_request(
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CliError(f"Could not load pull request #{pull_request_number}") from error

    if not review_identity.is_unlinked:
        expected_label = f"{review_identity.head_owner}:{review_identity.head_ref}"
        if (
            pull_request.head.ref == review_identity.head_ref
            and pull_request.head.label == expected_label
        ):
            return None

    lookups = await _find_managed_comments(
        github_client=github_client,
        pull_request_number=pull_request_number,
    )
    if not lookups:
        return None

    delete_actions: list[CleanupAction] = []
    delete_targets: list[tuple[int, StackCommentKind]] = []
    for lookup in lookups:
        if lookup.blocked_reason is not None:
            return StackCommentCleanupPlan(
                actions=(
                    CleanupAction(
                        kind=stack_comment_label(lookup.kind),
                        status="blocked",
                        body=lookup.blocked_reason,
                    ),
                )
            )
        if lookup.comment is None:
            continue
        delete_actions.append(
            CleanupAction(
                kind=stack_comment_label(lookup.kind),
                status="planned",
                body=(
                    f"delete {stack_comment_label(lookup.kind)} #{lookup.comment.id} from PR "
                    f"#{pull_request_number}"
                ),
            )
        )
        delete_targets.append((lookup.comment.id, lookup.kind))

    if not delete_actions:
        return None
    return StackCommentCleanupPlan(
        actions=tuple(delete_actions),
        comments=tuple(delete_targets),
    )


def _should_inspect_stack_comment_cleanup(
    *,
    remote: GitRemote | None,
    review_identity: ReviewIdentity,
    review_status: ReviewChangeStatus,
    stale_reason: str | None,
) -> bool:
    eligibility = _stack_comment_cleanup_eligibility(
        review_identity=review_identity,
        stale_reason=stale_reason,
    )
    if eligibility == "inspect":
        return True
    if eligibility == "skip":
        return False
    if remote is None:
        return False
    return review_status.remote_branch == "absent"


def _stack_comment_cleanup_eligibility(
    *,
    review_identity: ReviewIdentity,
    stale_reason: str | None,
) -> StackCommentCleanupEligibility:
    """Classify whether cleanup can inspect stack comments for this change.

    Stack comments may be deleted only when the PR no longer represents a live linked
    stack. Inspecting needs a locatable PR (a saved number, or the bookmark head for an
    unlinked change). A stale change first needs the remote branch confirmed absent
    ("needs-remote-check") before its comments are worth a live inspection.
    """

    if review_identity.is_unlinked:
        return "inspect"
    if stale_reason is None:
        return "inspect"
    return "needs-remote-check"
