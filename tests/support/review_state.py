"""Concise builders for explicit review-state components in tests."""

from __future__ import annotations

from jj_stack.models.review_state import (
    BookmarkOwnership,
    LinkState,
    ReviewIdentity,
)


def make_review_identity(
    *,
    bookmark_ownership: BookmarkOwnership = "managed",
    github_host: str = "github.test",
    head_owner: str = "octo-org",
    head_ref: str = "review/example",
    link_state: LinkState = "active",
    pr_number: int = 1,
    repository_name: str = "stacked-review",
    repository_owner: str = "octo-org",
) -> ReviewIdentity:
    """Build one complete nominal review identity."""

    return ReviewIdentity(
        github_host=github_host,
        repository_owner=repository_owner,
        repository_name=repository_name,
        pr_number=pr_number,
        head_owner=head_owner,
        head_ref=head_ref,
        bookmark_ownership=bookmark_ownership,
        link_state=link_state,
    )
