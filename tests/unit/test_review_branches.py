"""Stable remote review-branch naming and resolution."""

from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import (
    ResolvedReviewBranch,
    ensure_new_review_branches_unclaimed,
    ensure_unique_review_branches,
    resolve_review_branches,
)
from jj_stack.review_namespace import current_review_namespace, review_branch_matches_change


def test_generate_review_branch_normalizes_subject() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation!!!\n\nBody text.\n",
    )

    branch = current_review_namespace().generate_branch(revision)

    assert branch == "jj-stack/fix-cache-invalidation-zvlywqkx"


def test_generate_review_branch_falls_back_for_blank_subject() -> None:
    revision = _revision(change_id="abcdefghijklmno", description="\n")

    branch = current_review_namespace().generate_branch(revision)

    assert branch == "jj-stack/change-abcdefgh"


@pytest.mark.parametrize(
    ("branch", "matches"),
    (
        ("jj-stack/cache-fix-zvlywqkx", True),
        # The suffix ties a branch to its change; the rest of the name is not the matcher's
        # business, so a readable stem may hold anything and any namespace may carry the tie.
        ("jj-stack/cache_fix-zvlywqkx", True),
        ("team/cache-fix-zvlywqkx", True),
        ("jj-stack/cache-fix-abcdefgh", False),
        ("jj-stack/cache-fix-zvlywqkxtmnpqrstu", False),
    ),
)
def test_review_branch_matcher_ties_a_branch_to_one_change(
    branch: str,
    matches: bool,
) -> None:
    assert review_branch_matches_change(branch, "zvlywqkxtmnpqrstu") is matches


def test_review_branch_resolution_keeps_saved_branch_stable_after_subject_change() -> None:
    identities = {
        "zvlywqkxtmnpqrstu": _identity(head_ref="jj-stack/fix-cache-invalidation-zvlywqkx")
    }
    renamed_revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Rewrite cache invalidation from scratch\n",
    )

    resolutions = resolve_review_branches(
        revisions=(renamed_revision,),
        review_identities=identities,
    )

    assert resolutions[0].branch == "jj-stack/fix-cache-invalidation-zvlywqkx"


def test_review_branch_resolution_rejects_multiple_changes_on_same_branch() -> None:
    resolutions = (
        ResolvedReviewBranch(
            branch="jj-stack/shared-abcdefgh",
            change_id="abcdefghijklmno",
        ),
        ResolvedReviewBranch(
            branch="jj-stack/shared-abcdefgh",
            change_id="qrstuvwxyzabcde",
        ),
    )

    with pytest.raises(CliError, match="multiple changes to the same branch"):
        ensure_unique_review_branches(resolutions)


def test_review_branch_resolution_rejects_new_branch_claimed_by_another_stack() -> None:
    existing_change_id = "abcdefgh-one"
    new_change_id = "abcdefgh-two"
    branch = "jj-stack/shared-abcdefgh"

    identities = {existing_change_id: _identity(head_ref=branch)}
    resolutions = resolve_review_branches(
        revisions=(_revision(change_id=new_change_id, description="shared"),),
        review_identities=identities,
    )

    with pytest.raises(CliError, match="Cannot create a review on saved branch"):
        ensure_new_review_branches_unclaimed(
            resolutions,
            identities,
            ("octo-org", "stacked-review"),
        )

    ensure_new_review_branches_unclaimed(
        resolutions,
        {
            existing_change_id: identities[existing_change_id].model_copy(
                update={"repository_name": "another-repository"}
            )
        },
        ("octo-org", "stacked-review"),
    )


def _identity(*, head_ref: str) -> ReviewIdentity:
    return ReviewIdentity(
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
