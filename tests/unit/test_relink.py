from __future__ import annotations

import asyncio
from typing import cast

import pytest

from jj_stack.commands.relink import _load_exact_relink_pr
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import ChangeId
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead, PRState
from jj_stack.stack.pr_branches import require_unique_pr_claims
from tests.support.tracking import make_pr_identity


@pytest.mark.parametrize(
    ("head_owner", "state", "message"),
    (
        ("octo-org", "closed", "is not open"),
        ("someone-else", "open", "does not belong to octo-org/stacked-prs"),
    ),
)
def test_relink_requires_open_same_repo_pr(
    head_owner: str,
    state: PRState,
    message: str,
) -> None:
    pr = _pr(head_owner=head_owner, state=state)
    client = _GithubClientStub(pr)

    with pytest.raises(CliError, match=message):
        asyncio.run(
            _load_exact_relink_pr(
                github_client=cast(GithubClient, client),
                pr_number=1,
                repo=GithubRepoAddress(owner="octo-org", repo="stacked-prs"),
            )
        )


def test_duplicate_saved_pr_or_branch_claim_is_refused() -> None:
    branch = "jj-stack/manual-feature-feature1"

    with pytest.raises(CliError, match="already linked"):
        require_unique_pr_claims(
            saved={ChangeId("other-change"): make_pr_identity(head_ref=branch, pr_number=2)},
            replacements={
                ChangeId("feature1change"): make_pr_identity(head_ref=branch, pr_number=1)
            },
        )


class _GithubClientStub:
    def __init__(self, pr: GithubPR) -> None:
        self.pr = pr

    async def get_pr(self, *, pr_number: int) -> GithubPR:
        assert pr_number == self.pr.number
        return self.pr


def _pr(*, head_owner: str, state: PRState) -> GithubPR:
    branch = "jj-stack/manual-feature-feature1"
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubPRHead(
            label=f"{head_owner}:{branch}",
            ref=branch,
            sha="feature1commit",
        ),
        html_url="https://github.test/octo-org/stacked-prs/pull/1",
        node_id="PR_1",
        number=1,
        state=state,
        title="manual title",
    )
