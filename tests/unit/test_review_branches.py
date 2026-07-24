"""Stable remote review-branch naming and resolution."""

from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import (
    ResolvedReviewBranch,
    ensure_unique_review_branches,
    generate_review_branch,
    resolve_review_branches,
    restarted_review_branch,
    review_branch_matches_change,
)


def test_generate_review_branch_normalizes_subject() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation!!!\n\nBody text.\n",
    )

    assert generate_review_branch(revision) == "review/fix-cache-invalidation-zvlywqkx"


def test_generate_review_branch_falls_back_for_blank_subject() -> None:
    revision = _revision(change_id="abcdefghijklmno", description="\n")

    assert generate_review_branch(revision) == "review/change-abcdefgh"


@pytest.mark.parametrize(
    ("branch", "matches"),
    (
        ("review/cache-fix-zvlywqkx", True),
        ("review/cache-fix-fresh-pr42-zvlywqkx", True),
        ("team/cache-fix-zvlywqkx", False),
        ("review/cache_fix-zvlywqkx", False),
        ("review/cache-fix-abcdefgh", False),
        ("review/-zvlywqkx", False),
        ("review/fresh-pr17-zvlywqkx", False),
        ("review/cache-fix-fresh-pr17-fresh-pr18-zvlywqkx", False),
    ),
)
def test_review_branch_matcher_enforces_managed_grammar(
    branch: str,
    matches: bool,
) -> None:
    assert review_branch_matches_change(branch, "zvlywqkxtmnpqrstu") is matches


def test_restarted_review_branch_replaces_prior_marker() -> None:
    assert (
        restarted_review_branch(
            change_id="zvlywqkxtmnpqrstu",
            previous_branch="review/cache-fix-fresh-pr42-zvlywqkx",
            previous_pull_request=57,
        )
        == "review/cache-fix-fresh-pr57-zvlywqkx"
    )


def test_generate_review_branch_disambiguates_reserved_restart_marker() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="fresh pr42\n",
    )

    assert generate_review_branch(revision) == "review/fresh-pr42-change-zvlywqkx"


def test_review_branch_resolution_generates_branch_when_no_identity_exists() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation\n",
    )

    resolutions = resolve_review_branches(
        revisions=(revision,),
        review_identities={},
    )

    assert resolutions[0].branch == "review/fix-cache-invalidation-zvlywqkx"


def test_review_branch_resolution_keeps_saved_branch_stable_after_subject_change() -> None:
    identities = {
        "zvlywqkxtmnpqrstu": _identity(head_ref="review/fix-cache-invalidation-zvlywqkx")
    }
    renamed_revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Rewrite cache invalidation from scratch\n",
    )

    resolutions = resolve_review_branches(
        revisions=(renamed_revision,),
        review_identities=identities,
    )

    assert resolutions[0].branch == "review/fix-cache-invalidation-zvlywqkx"


def test_review_branch_resolution_rejects_multiple_changes_on_same_branch() -> None:
    resolutions = (
        ResolvedReviewBranch(
            branch="review/shared-abcdefgh",
            change_id="abcdefghijklmno",
        ),
        ResolvedReviewBranch(
            branch="review/shared-abcdefgh",
            change_id="qrstuvwxyzabcde",
        ),
    )

    with pytest.raises(CliError, match="multiple changes to the same branch"):
        ensure_unique_review_branches(resolutions)


def _identity(*, head_ref: str) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=1,
        head_owner="octo-org",
        head_ref=head_ref,
    )


def _revision(*, change_id: str, description: str) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=f"{change_id}-commit",
        current_working_copy=False,
        description=description,
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("parent",),
    )
