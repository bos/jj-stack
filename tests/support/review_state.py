"""Concise builders for explicit review-state components in tests."""

from __future__ import annotations

from jj_stack.models.review_state import ReviewIdentity


def make_review_identity(
    *,
    github_host: str = "github.test",
    head_owner: str = "octo-org",
    head_ref: str = "review/example-abcdefgh",
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
    )
