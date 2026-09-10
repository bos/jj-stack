from __future__ import annotations

from jj_stack.stack.pr_branches import duplicate_pr_claim_change_ids
from tests.support.tracking import make_pr_identity

BRANCH = "jj-stack/feature-aaaaaaaa"


def test_duplicate_claim_facts_reject_shared_prs_and_branches() -> None:
    identity = make_pr_identity(head_ref=BRANCH)
    same_pr = identity.model_copy(update={"head_ref": "jj-stack/other-bbbbbbbb"})
    same_branch = identity.model_copy(update={"pr_number": 2})

    assert duplicate_pr_claim_change_ids({"saved": identity, "same-pr": same_pr}) == frozenset(
        {"saved", "same-pr"}
    )
    assert duplicate_pr_claim_change_ids(
        {"saved": identity, "same-branch": same_branch}
    ) == frozenset({"saved", "same-branch"})
