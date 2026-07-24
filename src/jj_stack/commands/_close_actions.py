"""Shared types and rendering helpers for close command action rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import (
    StackCommentKind,
    comment_matches_kind,
    stack_comment_label,
)
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.github import GithubIssueComment
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.review.bookmarks import (
    bookmark_cleanup_allowed,
    classify_local_bookmark_forget,
    local_bookmark_forget_blocked_body,
)
from jj_stack.review.change_status import classify_review_change
from jj_stack.ui import Message, plain_text

ActionPresentationStatus = Literal["applied", "blocked", "planned", "skipped"]
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
    def dry_run(self) -> bool: ...

    @property
    def jj_client(self) -> JjClient: ...


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


@dataclass(frozen=True, slots=True)
class ManagedCommentLookup:
    """One resolution result for a managed stack comment on a pull request.

    Exactly one of ``comment`` or ``blocked_reason`` is set; the other is
    ``None``. Kinds with neither a cached id nor a body-marker match are
    omitted from the result rather than represented as a third state.
    """

    kind: StackCommentKind
    comment: GithubIssueComment | None = None
    blocked_reason: str | None = None


async def find_managed_comments(
    *,
    github_client: GithubClient,
    pull_request_number: int,
) -> tuple[ManagedCommentLookup, ...]:
    """Discover managed stack comments for one PR via a single list call.

    Managed comments are identified by their body markers alone. Returns
    entries only for kinds that resolved to a delete target or were blocked;
    kinds with no body-marker match are omitted.
    """

    kinds: tuple[StackCommentKind, ...] = ("navigation", "overview")

    try:
        comments = await github_client.list_issue_comments(
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return ()
        reason = error.user_facing_reason()
        return tuple(
            ManagedCommentLookup(
                kind=kind,
                blocked_reason=(
                    f"cannot inspect {stack_comment_label(kind)}s for PR "
                    f"#{pull_request_number}: {reason}"
                ),
            )
            for kind in kinds
        )

    return tuple(
        entry
        for kind in kinds
        for entry in (
            _resolve_managed_comment_from_listed(
                comments=comments,
                kind=kind,
                pull_request_number=pull_request_number,
            ),
        )
        if entry is not None
    )


def _resolve_managed_comment_from_listed(
    *,
    comments: tuple[GithubIssueComment, ...],
    kind: StackCommentKind,
    pull_request_number: int,
) -> ManagedCommentLookup | None:
    matching_comments = [
        comment for comment in comments if comment_matches_kind(body=comment.body, kind=kind)
    ]
    if len(matching_comments) > 1:
        return ManagedCommentLookup(
            kind=kind,
            blocked_reason=(
                f"cannot delete {stack_comment_label(kind)}s because GitHub reports "
                f"multiple candidates on PR #{pull_request_number}"
            ),
        )
    if not matching_comments:
        return None
    return ManagedCommentLookup(kind=kind, comment=matching_comments[0])


def emit_close_actions(
    *,
    actions: tuple[CloseAction, ...],
    applied: bool,
    blocked: bool,
) -> None:
    header = (
        "Close blocked:"
        if blocked
        else ("Applied close actions:" if applied else "Planned close actions:")
    )
    console.output(header)
    for action in actions:
        emit_action_row(kind=action.kind, status=action.status, body=action.body)


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


def retire_review_identity(review_identity: ReviewIdentity) -> ReviewIdentity:
    # A closed review keeps its identity but is unlinked: list stops reporting
    # it as an open orphan, submit will not silently reattach it, and cleanup
    # can still locate its artifacts. relink re-adopts it explicitly.
    return review_identity.model_copy(update={"link_state": "unlinked"})


def plan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    review_identity: ReviewIdentity,
    cleanup_user_bookmarks: bool,
    commit_id: str | None,
    prefix: str,
    record_action: Callable[[CloseAction], None],
    remote_name: str | None,
) -> BookmarkCleanupPlan:
    """Validate bookmark ownership and decide which cleanup mutations are safe."""

    if not bookmark_cleanup_allowed(
        bookmark=bookmark,
        bookmark_managed=review_identity.manages_bookmark,
        cleanup_user_bookmarks=cleanup_user_bookmarks,
        prefix=prefix,
    ):
        return BookmarkCleanupPlan(local_forget=False, remote_delete=False)

    local_forget = False
    remote_delete = False
    local_conflict = False
    remote_conflict = False
    branch_label = f"{bookmark}@{remote_name}" if remote_name is not None else bookmark

    match classify_local_bookmark_forget(
        bookmark_state=bookmark_state,
        expected_commit_id=commit_id,
    ):
        case "conflicted" | "diverged" as local_safety:
            record_action(
                CloseAction(
                    kind="local bookmark",
                    body=local_bookmark_forget_blocked_body(bookmark, local_safety),
                    status="blocked",
                )
            )
            local_conflict = True
        case "safe":
            local_forget = True
        case _:
            pass

    remote_state = bookmark_state.remote_target(remote_name) if remote_name is not None else None
    if commit_id is not None:
        review_status = classify_review_change(
            commit_id=commit_id,
            local="orphaned",
            pull_request_lookup=None,
            remote_state=remote_state,
            review_identity=review_identity,
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
