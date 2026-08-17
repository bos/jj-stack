"""Stable remote PR branch naming and resolution."""

from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import PRIdentity
from jj_stack.pr_branch_namespace import current_pr_branch_namespace, pr_branch_matches_change
from jj_stack.stack.pr_branches import (
    ResolvedPRBranch,
    ensure_new_pr_branches_unclaimed,
    ensure_unique_pr_branches,
    resolve_pr_branches,
)


def test_generate_pr_branch_normalizes_subject() -> None:
    change = _change(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation!!!\n\nBody text.\n",
    )

    branch = current_pr_branch_namespace().generate_branch(change)

    assert branch == "jj-stack/fix-cache-invalidation-zvlywqkx"


def test_generate_pr_branch_falls_back_for_blank_subject() -> None:
    change = _change(change_id="abcdefghijklmno", description="\n")

    branch = current_pr_branch_namespace().generate_branch(change)

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
def test_pr_branch_matcher_ties_a_branch_to_one_change(
    branch: str,
    matches: bool,
) -> None:
    assert pr_branch_matches_change(branch, "zvlywqkxtmnpqrstu") is matches


def test_pr_branch_resolution_keeps_saved_branch_stable_after_subject_change() -> None:
    identities = {
        "zvlywqkxtmnpqrstu": _identity(head_ref="jj-stack/fix-cache-invalidation-zvlywqkx")
    }
    renamed_change = _change(
        change_id="zvlywqkxtmnpqrstu",
        description="Rewrite cache invalidation from scratch\n",
    )

    resolutions = resolve_pr_branches(
        changes=(renamed_change,),
        pr_identities=identities,
    )

    assert resolutions[0].branch == "jj-stack/fix-cache-invalidation-zvlywqkx"


def test_pr_branch_resolution_rejects_multiple_changes_on_same_branch() -> None:
    resolutions = (
        ResolvedPRBranch(
            branch="jj-stack/shared-abcdefgh",
            change_id="abcdefghijklmno",
        ),
        ResolvedPRBranch(
            branch="jj-stack/shared-abcdefgh",
            change_id="qrstuvwxyzabcde",
        ),
    )

    with pytest.raises(CliError, match="multiple changes to the same branch"):
        ensure_unique_pr_branches(resolutions)


def test_pr_branch_resolution_rejects_new_branch_claimed_by_another_stack() -> None:
    existing_change_id = "abcdefgh-one"
    new_change_id = "abcdefgh-two"
    branch = "jj-stack/shared-abcdefgh"

    identities = {existing_change_id: _identity(head_ref=branch)}
    resolutions = resolve_pr_branches(
        changes=(_change(change_id=new_change_id, description="shared"),),
        pr_identities=identities,
    )

    with pytest.raises(CliError, match="Cannot create a pull request on saved PR branch"):
        ensure_new_pr_branches_unclaimed(
            resolutions,
            identities,
            ("octo-org", "stacked-prs"),
        )

    ensure_new_pr_branches_unclaimed(
        resolutions,
        {
            existing_change_id: identities[existing_change_id].model_copy(
                update={"repo_name": "another-repository"}
            )
        },
        ("octo-org", "stacked-prs"),
    )


def _identity(*, head_ref: str) -> PRIdentity:
    return PRIdentity(
        repo_owner="octo-org",
        repo_name="stacked-prs",
        pr_number=1,
        head_owner="octo-org",
        head_ref=head_ref,
    )


def _change(*, change_id: str, description: str) -> LocalCommit:
    return LocalCommit(
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
