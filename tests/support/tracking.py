"""Concise builders for explicit tracking-state components in tests."""

from __future__ import annotations

from jj_stack.models.tracking import PRIdentity


def make_pr_identity(
    *,
    head_owner: str = "octo-org",
    head_ref: str = "jj-stack/example-abcdefgh",
    pr_number: int = 1,
    repo_name: str = "stacked-prs",
    repo_owner: str = "octo-org",
) -> PRIdentity:
    """Build one complete nominal PR identity."""

    return PRIdentity(
        repo_owner=repo_owner,
        repo_name=repo_name,
        pr_number=pr_number,
        head_owner=head_owner,
        head_ref=head_ref,
    )
