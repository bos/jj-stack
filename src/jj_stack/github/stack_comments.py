"""Shared helpers for GitHub stack navigation and overview comments."""

from __future__ import annotations

from typing import Literal

from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError

StackCommentKind = Literal["navigation", "overview"]

STACK_NAVIGATION_COMMENT_MARKER = "<!-- jj-stack-navigation -->"
STACK_OVERVIEW_COMMENT_MARKER = "<!-- jj-stack-overview -->"


def stack_comment_marker(kind: StackCommentKind) -> str:
    """Return the marker used for one managed comment kind."""

    if kind == "navigation":
        return STACK_NAVIGATION_COMMENT_MARKER
    return STACK_OVERVIEW_COMMENT_MARKER


def stack_comment_label(kind: StackCommentKind) -> str:
    """Return a user-facing label for one managed comment kind."""

    if kind == "navigation":
        return "stack navigation comment"
    return "stack overview comment"


def is_navigation_comment(body: str) -> bool:
    """Return whether a GitHub comment body is a managed navigation comment."""

    return STACK_NAVIGATION_COMMENT_MARKER in body


def is_overview_comment(body: str) -> bool:
    """Return whether a GitHub comment body is a managed overview comment."""

    return STACK_OVERVIEW_COMMENT_MARKER in body


def is_stack_summary_comment(body: str) -> bool:
    """Return whether a GitHub comment body belongs to jj-stack."""

    return is_navigation_comment(body) or is_overview_comment(body)


async def delete_stack_comment(
    *,
    comment_id: int,
    github_client: GithubClient,
    kind: StackCommentKind,
) -> None:
    """Delete one managed stack comment, tolerating an already-deleted target."""

    try:
        await github_client.delete_issue_comment(
            comment_id=comment_id,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return
        raise CliError(
            f"Could not delete {stack_comment_label(kind)} #{comment_id}"
        ) from error
