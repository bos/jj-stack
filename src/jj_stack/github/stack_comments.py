"""Shared helpers for GitHub stack navigation and overview comments."""

from __future__ import annotations

from typing import Literal

StackCommentKind = Literal["navigation", "overview"]

STACK_NAVIGATION_COMMENT_MARKER = "<!-- jj-review-navigation -->"
STACK_OVERVIEW_COMMENT_MARKER = "<!-- jj-review-overview -->"


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
    """Return whether a GitHub comment body belongs to jj-review."""

    return is_navigation_comment(body) or is_overview_comment(body)
