"""Stable remote PR branch naming and resolution."""

from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.identifiers import ChangeId
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import SubmittedBaseline, TrackedPR
from jj_stack.pr_branch_namespace import (
    PRBranchNamespace,
    current_pr_branch_namespace,
    pr_branch_matches_change,
)
from jj_stack.stack.pr_branches import (
    ResolvedPRBranch,
    ensure_new_pr_branches_unclaimed,
    ensure_unique_pr_branches,
    resolve_pr_branches,
)
from tests.support.tracking import make_pr_identity


def test_generate_pr_branch_normalizes_subject() -> None:
    change = _change(
        change_id=ChangeId("zvlywqkxtmnpqrstu"),
        description="Fix cache invalidation!!!\n\nBody text.\n",
    )

    branch = current_pr_branch_namespace().generate_branch(change)

    assert branch == "jj-stack/fix-cache-invalidation-zvlywqkx"


def test_generate_pr_branch_falls_back_when_subject_has_no_ascii_slug() -> None:
    change = _change(change_id=ChangeId("abcdefghijklmno"), description="修正 🚀\n")

    branch = current_pr_branch_namespace().generate_branch(change)

    assert branch == "jj-stack/change-abcdefgh"


def test_generate_pr_branch_truncates_a_subject_github_cannot_store() -> None:
    change = _change(
        change_id=ChangeId("zvlywqkxtmnpqrstu"),
        description=" ".join(["refactor the transport layer"] * 12) + "\n",
    )

    branch = current_pr_branch_namespace().generate_branch(change)

    assert len(f"refs/heads/{branch}".encode()) <= 255
    assert branch.startswith("jj-stack/refactor-the-transport-layer")
    assert branch.endswith("-zvlywqkx")
    assert "--" not in branch
    # GitHub counts a ref's bytes, and a configured prefix may hold multibyte characters.
    non_ascii_prefix = PRBranchNamespace("préfixe").generate_branch(change)
    assert len(f"refs/heads/{non_ascii_prefix}".encode()) <= 255


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
    assert pr_branch_matches_change(branch, ChangeId("zvlywqkxtmnpqrstu")) is matches


def test_pr_branch_resolution_rejects_multiple_changes_on_same_branch() -> None:
    resolutions = (
        ResolvedPRBranch(
            branch="jj-stack/shared-abcdefgh",
            change_id=ChangeId("abcdefghijklmno"),
        ),
        ResolvedPRBranch(
            branch="jj-stack/shared-abcdefgh",
            change_id=ChangeId("qrstuvwxyzabcde"),
        ),
    )

    with pytest.raises(CliError, match="same PR branch"):
        ensure_unique_pr_branches(resolutions)


def test_pr_branch_resolution_rejects_new_branch_claimed_by_another_stack() -> None:
    existing_change_id = ChangeId("abcdefgh-one")
    new_change_id = ChangeId("abcdefgh-two")
    branch = "jj-stack/shared-abcdefgh"

    tracked_prs = {
        existing_change_id: TrackedPR(
            pr_identity=make_pr_identity(head_ref=branch),
            submitted_baseline=SubmittedBaseline(commit_id="submitted"),
        )
    }
    resolutions = resolve_pr_branches(
        changes=(_change(change_id=new_change_id, description="shared"),),
        tracked_prs=tracked_prs,
    )

    with pytest.raises(CliError, match="already linked to other changes"):
        ensure_new_pr_branches_unclaimed(
            resolutions,
            tracked_prs,
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
        signed=False,
    )
