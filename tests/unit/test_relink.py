from __future__ import annotations

import asyncio
from typing import cast

import pytest

from jj_stack.commands.relink import (
    _ensure_relinkable_cached_link,
    _load_exact_relink_pull_request,
)
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState


@pytest.mark.parametrize(
    ("head_owner", "state", "message"),
    (
        ("octo-org", "closed", "is not open"),
        ("someone-else", "open", "does not belong to the configured repository"),
    ),
)
def test_relink_requires_open_same_repository_pull_request(
    head_owner: str,
    state: str,
    message: str,
) -> None:
    pull_request = _pull_request(head_owner=head_owner, state=state)
    client = _GithubClientStub(pull_request)

    with pytest.raises(CliError, match=message):
        asyncio.run(
            _load_exact_relink_pull_request(
                change_id="feature1change",
                github_client=cast(GithubClient, client),
                pull_number=1,
                repository_owner="octo-org",
            )
        )


def test_relink_rejects_duplicate_saved_pr_or_branch_claim_in_same_repository() -> None:
    identity = _identity(pr_number=1)
    state = ReviewState(
        review_identities={
            "other-change": _identity(pr_number=2),
        }
    )

    with pytest.raises(CliError, match="already linked"):
        _ensure_relinkable_cached_link(
            change_id="feature1change",
            identity=identity,
            state=state,
        )


def test_relink_duplicate_claim_check_is_scoped_to_repository() -> None:
    identity = _identity(pr_number=1)
    other_repository = ReviewIdentity(
        github_host=identity.github_host,
        repository_owner="another-org",
        repository_name=identity.repository_name,
        pr_number=identity.pr_number,
        head_owner=identity.head_owner,
        head_ref=identity.head_ref,
    )

    _ensure_relinkable_cached_link(
        change_id="feature1change",
        identity=identity,
        state=ReviewState(review_identities={"other-change": other_repository}),
    )


class _GithubClientStub:
    def __init__(self, pull_request: GithubPullRequest) -> None:
        self.pull_request = pull_request

    async def get_pull_request(self, *, pull_number: int) -> GithubPullRequest:
        assert pull_number == self.pull_request.number
        return self.pull_request

    async def get_pull_requests_by_head_refs(
        self,
        *,
        head_refs: tuple[str, ...],
    ) -> dict[str, tuple[GithubPullRequest, ...]]:
        assert head_refs == (self.pull_request.head.ref,)
        return {self.pull_request.head.ref: (self.pull_request,)}


def _pull_request(*, head_owner: str, state: str) -> GithubPullRequest:
    branch = "review/manual-feature-feature1"
    return GithubPullRequest(
        base=GithubBranchRef(label="octo-org:main", ref="main"),
        head=GithubBranchRef(
            label=f"{head_owner}:{branch}",
            ref=branch,
            sha="feature1commit",
        ),
        html_url="https://github.test/octo-org/stacked-review/pull/1",
        number=1,
        state=state,
        title="manual title",
    )


def _identity(*, pr_number: int) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref="review/manual-feature-feature1",
    )
