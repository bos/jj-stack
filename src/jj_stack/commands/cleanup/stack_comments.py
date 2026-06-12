"""Stack-comment cleanup planning and execution for the plain cleanup pass."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.commands._close_actions import find_managed_comments as _find_managed_comments
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import (
    StackCommentKind,
    stack_comment_label,
)
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.review_state import CachedChange
from jj_stack.review.change_status import ReviewChangeStatus
from jj_stack.state.journal import OperationJournal

from .shared import (
    CleanupAction,
    PreparedCleanup,
    PreparedCleanupChange,
    _CleanupSaver,
)

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
    journal: OperationJournal,
    next_changes: dict[str, CachedChange],
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    saver: _CleanupSaver,
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
                cached_change=prepared_change.cached_change,
                bookmark_state=prepared_change.bookmark_state,
                github_client=github_client,
            ),
            on_success=lambda _index, _result: progress.advance(),
        )
    for prepared_change, comment_plan in zip(
        stack_comment_changes,
        comment_plans,
        strict=True,
    ):
        if comment_plan is None:
            continue
        await _apply_stack_comment_cleanup_action(
            comment_plan=comment_plan,
            change_id=prepared_change.change_id,
            github_client=github_client,
            journal=journal,
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
            saver=saver,
        )


async def _apply_stack_comment_cleanup_action(
    *,
    comment_plan: StackCommentCleanupPlan,
    change_id: str,
    github_client: GithubClient,
    journal: OperationJournal,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    saver: _CleanupSaver,
) -> None:
    applied_comments = False
    targeted_actions = comment_plan.actions[: len(comment_plan.comments)]
    for action, (comment_id, kind) in zip(
        targeted_actions,
        comment_plan.comments,
        strict=True,
    ):
        comment_action = action
        if not prepared_cleanup.dry_run and comment_action.status == "planned":
            with journal.mutation(
                "delete_issue_comment",
                change_id=change_id,
                comment_id=comment_id,
                kind=kind,
            ):
                try:
                    await github_client.delete_issue_comment(
                        comment_id=comment_id,
                    )
                except GithubClientError as error:
                    raise CliError(
                        f"Could not delete {stack_comment_label(kind)} #{comment_id}"
                    ) from error
            applied_comments = True
            comment_action = replace(action, status="applied")
        record_action(comment_action)
    for action in comment_plan.actions[len(targeted_actions) :]:
        record_action(action)
    if applied_comments and change_id in next_changes:
        next_changes[change_id] = next_changes[change_id].with_cleared_comments()
    saver.save_if_changed(next_changes)


async def _plan_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
) -> StackCommentCleanupPlan | None:
    pull_request_number = cached_change.pr_number
    if pull_request_number is None and cached_change.is_unlinked:
        pull_request_number = await _resolve_unlinked_pull_request_number(
            bookmark_state=bookmark_state,
            github_client=github_client,
        )
        if isinstance(pull_request_number, CleanupAction):
            return StackCommentCleanupPlan(actions=(pull_request_number,))

    if pull_request_number is None:
        return None

    try:
        pull_request = await github_client.get_pull_request(
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CliError(f"Could not load pull request #{pull_request_number}") from error

    if not cached_change.is_unlinked:
        bookmark = cached_change.bookmark
        if bookmark is None:
            return None
        expected_label = f"{github_client.repository.owner}:{bookmark}"
        if pull_request.head.ref == bookmark and pull_request.head.label == expected_label:
            return None

    lookups = await _find_managed_comments(
        cached_navigation_comment_id=cached_change.navigation_comment_id,
        cached_overview_comment_id=cached_change.overview_comment_id,
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


async def _resolve_unlinked_pull_request_number(
    *,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
) -> int | CleanupAction | None:
    if bookmark_state.name == "":
        return None

    try:
        pull_requests = await github_client.list_pull_requests(
            head=f"{github_client.repository.owner}:{bookmark_state.name}",
            state="all",
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not list pull requests for unlinked bookmark "
            t"{ui.bookmark(bookmark_state.name)}"
        ) from error

    if not pull_requests:
        return None
    if len(pull_requests) > 1:
        return CleanupAction(
            kind="stack navigation comment",
            status="blocked",
            body=(
                t"cannot delete stack navigation comments because GitHub reports multiple "
                t"pull requests for unlinked bookmark {ui.bookmark(bookmark_state.name)}"
            ),
        )
    return pull_requests[0].number


def _should_inspect_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    remote: GitRemote | None,
    review_status: ReviewChangeStatus,
    stale_reason: str | None,
) -> bool:
    eligibility = _stack_comment_cleanup_eligibility(
        cached_change=cached_change,
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
    cached_change: CachedChange,
    stale_reason: str | None,
) -> StackCommentCleanupEligibility:
    """Classify whether cleanup can inspect stack comments for this change.

    Stack comments may be deleted only when the PR no longer represents a live linked
    stack. Inspecting needs a locatable PR (a saved number, or the bookmark head for an
    unlinked change) plus evidence worth checking: the change is unlinked, its live PR
    head may have drifted off the tracked bookmark, or stale tracking still carries
    cached comment ids. A stale change whose only lead is its bookmark first needs the
    remote branch confirmed absent ("needs-remote-check").
    """

    if cached_change.is_unlinked:
        if cached_change.pr_number is None and cached_change.bookmark is None:
            return "skip"
        return "inspect"
    if cached_change.pr_number is None:
        return "skip"
    has_cached_comment_ids = (
        cached_change.navigation_comment_id is not None
        or cached_change.overview_comment_id is not None
    )
    if cached_change.bookmark is None and not has_cached_comment_ids:
        return "skip"
    if stale_reason is None:
        return "inspect"
    if cached_change.pr_state in {"closed", "merged"}:
        return "skip"
    if has_cached_comment_ids:
        return "inspect"
    return "needs-remote-check"
