"""Shared types and rendering helpers for close command action rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from jj_review import ui
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.error_messages import summarize_github_error_reason
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.github.stack_comments import (
    StackCommentKind,
    is_navigation_comment,
    is_overview_comment,
    stack_comment_label,
)
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.github import GithubIssueComment
from jj_review.models.review_state import CachedChange
from jj_review.review.bookmarks import is_review_bookmark
from jj_review.review.change_status import classify_review_change
from jj_review.ui import Message, plain_text

CloseActionStatus = Literal["applied", "blocked", "planned"]
type CloseActionBody = Message


@dataclass(frozen=True, slots=True)
class BookmarkCleanupPlan:
    """Resolved bookmark cleanup actions for one cached change."""

    local_forget: bool
    remote_delete: bool


class BookmarkCleanupRun(Protocol):
    """Execution state needed to apply bookmark cleanup mutations."""

    @property
    def dry_run(self) -> bool:
        ...

    @property
    def jj_client(self) -> JjClient:
        ...


@dataclass(frozen=True, slots=True)
class CloseAction:
    """One close action that was planned, applied, or blocked."""

    kind: str
    status: CloseActionStatus
    body: CloseActionBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


NAVIGATION_COMMENT_KIND = stack_comment_label("navigation")
OVERVIEW_COMMENT_KIND = stack_comment_label("overview")


def comment_matches_kind(*, body: str, kind: StackCommentKind) -> bool:
    if kind == "navigation":
        return is_navigation_comment(body)
    return is_overview_comment(body)


async def find_managed_comment(
    *,
    cached_comment_id: int | None,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    kind: StackCommentKind,
    pull_request_number: int,
) -> tuple[GithubIssueComment | None, CloseAction | None]:
    """Resolve the saved jj-review stack comment for a PR, if any."""

    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            if cached_comment_id is None:
                return None, None
            try:
                cached_comment = await github_client.get_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=cached_comment_id,
                )
            except GithubClientError as cached_comment_error:
                if cached_comment_error.status_code == 404:
                    return None, None
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot inspect saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id}: "
                            f"{summarize_github_error_reason(cached_comment_error)}"
                        ),
                        status="blocked",
                    ),
                )
            if not comment_matches_kind(body=cached_comment.body, kind=kind):
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot delete saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id} because it does not belong to "
                            "jj-review"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None
        return (
            None,
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"cannot inspect {stack_comment_label(kind)}s for PR "
                    f"#{pull_request_number}: {summarize_github_error_reason(error)}"
                ),
                status="blocked",
            ),
        )

    if cached_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_comment_id),
            None,
        )
        if cached_comment is not None:
            if not comment_matches_kind(body=cached_comment.body, kind=kind):
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot delete saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id} because it does not belong to "
                            "jj-review"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None

    matching_comments = [
        comment for comment in comments if comment_matches_kind(body=comment.body, kind=kind)
    ]
    if len(matching_comments) > 1:
        return (
            None,
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"cannot delete {stack_comment_label(kind)}s because GitHub reports "
                    f"multiple candidates on PR #{pull_request_number}"
                ),
                status="blocked",
            ),
        )
    if not matching_comments:
        return None, None
    return matching_comments[0], None


def render_close_action_message(action: CloseAction) -> CloseActionBody:
    if action.kind == "tracking":
        return action.body
    return (ui.semantic_text(action.kind, "prefix"), ": ", action.body)


def close_action_presentation(
    status: CloseActionStatus,
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
    return ("  ?", None, None)


def retire_cached_change(
    cached_change: CachedChange,
    *,
    pr_state: str,
) -> CachedChange:
    # Closed changes remain "active" unless they were explicitly unlinked. The saved
    # jj-review data still needs the last known review identity so later cleanup or
    # status refresh can reason about the already-closed stack without reattaching it.
    updates: dict[str, object] = {
        "pr_review_decision": None,
        "pr_state": pr_state,
    }
    return cached_change.model_copy(update=updates)


def plan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    cleanup_user_bookmarks: bool,
    commit_id: str | None,
    prefix: str,
    record_action: Callable[[CloseAction], None],
    remote_name: str | None,
) -> BookmarkCleanupPlan:
    """Validate bookmark ownership and decide which cleanup mutations are safe."""

    if cached_change.manages_bookmark:
        if not is_review_bookmark(bookmark, prefix=prefix):
            return BookmarkCleanupPlan(local_forget=False, remote_delete=False)
    elif not cleanup_user_bookmarks:
        return BookmarkCleanupPlan(local_forget=False, remote_delete=False)

    local_forget = False
    remote_delete = False
    local_conflict = False
    remote_conflict = False
    local_target = bookmark_state.local_target
    branch_label = f"{bookmark}@{remote_name}" if remote_name is not None else bookmark

    if len(bookmark_state.local_targets) > 1:
        record_action(
            CloseAction(
                kind="local bookmark",
                body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
                status="blocked",
            )
        )
        local_conflict = True
    elif commit_id is not None and local_target is not None and local_target != commit_id:
        record_action(
            CloseAction(
                kind="local bookmark",
                body=t"cannot forget {ui.bookmark(bookmark)} because it already points "
                t"to a different revision",
                status="blocked",
            )
        )
        local_conflict = True
    elif commit_id is not None and local_target == commit_id:
        local_forget = True

    remote_state = bookmark_state.remote_target(remote_name) if remote_name is not None else None
    if commit_id is not None:
        review_status = classify_review_change(
            cached_change=cached_change,
            commit_id=commit_id,
            local="orphaned",
            pull_request_lookup=None,
            remote_state=remote_state,
        )
        if review_status.remote_branch == "conflicted":
            record_action(
                CloseAction(
                    kind="remote branch",
                    body=t"cannot delete {ui.bookmark(branch_label)} because the remote "
                    t"bookmark is conflicted",
                    status="blocked",
                )
            )
            remote_conflict = True
        elif (
            review_status.remote_branch != "absent"
            and review_status.remote_branch_matches_commit is not True
        ):
            record_action(
                CloseAction(
                    kind="remote branch",
                    body=t"cannot delete {ui.bookmark(branch_label)} because it already "
                    t"points to a different revision",
                    status="blocked",
                )
            )
            remote_conflict = True
        elif review_status.remote_branch_matches_commit is True:
            remote_delete = True

    if local_conflict:
        remote_delete = False
    if remote_conflict:
        local_forget = False
    return BookmarkCleanupPlan(
        local_forget=local_forget,
        remote_delete=remote_delete,
    )


def apply_bookmark_cleanup(
    *,
    bookmark: str,
    cleanup_plan: BookmarkCleanupPlan,
    commit_id: str | None,
    record_action: Callable[[CloseAction], None],
    remote_name: str | None,
    run: BookmarkCleanupRun,
) -> None:
    """Record and optionally execute validated bookmark cleanup mutations."""

    dry_run = run.dry_run
    if cleanup_plan.remote_delete:
        branch_label = f"{bookmark}@{remote_name}" if remote_name is not None else bookmark
        record_action(
            CloseAction(
                kind="remote branch",
                body=t"delete {ui.bookmark(branch_label)}",
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            if remote_name is None or commit_id is None:
                raise AssertionError("Planned remote branch deletion requires a target.")
            run.jj_client.delete_remote_bookmarks(
                remote=remote_name,
                deletions=((bookmark, commit_id),),
            )
    if cleanup_plan.local_forget:
        record_action(
            CloseAction(
                kind="local bookmark",
                body=t"forget {ui.bookmark(bookmark)}",
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            run.jj_client.forget_bookmarks((bookmark,))
