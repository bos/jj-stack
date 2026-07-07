from __future__ import annotations

import asyncio
from typing import cast

from jj_stack.commands._close_actions import CloseAction
from jj_stack.commands.close_orphan import (
    _lookup_orphaned_pull_request,
    _OrphanedPullRequestInspection,
)
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import CachedChange

_BOOKMARK = "review/feature-aaaaaaaa"
_OWNER = "octo-org"


def _pull_request(
    *,
    head_label: str | None = None,
    head_ref: str = _BOOKMARK,
    number: int = 1,
    state: str = "open",
) -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(label=f"{_OWNER}:main", ref="main"),
        head=GithubBranchRef(
            label=f"{_OWNER}:{head_ref}" if head_label is None else head_label,
            ref=head_ref,
        ),
        html_url=f"https://github.test/{_OWNER}/stacked-review/pull/{number}",
        number=number,
        state=state,
        title="feature 1",
    )


class _GithubClientStub:
    def __init__(
        self,
        *,
        branch_matches: dict[str, tuple[GithubPullRequest, ...]] | None = None,
        lookup_error: GithubClientError | None = None,
        pull_request: GithubPullRequest | None = None,
    ) -> None:
        self.repository = GithubRepoAddress(
            host="github.test",
            owner=_OWNER,
            repo="stacked-review",
        )
        self._branch_matches = branch_matches or {}
        self._lookup_error = lookup_error
        self._pull_request = pull_request

    async def get_pull_request(self, *, pull_number: int) -> GithubPullRequest:
        if self._lookup_error is not None:
            raise self._lookup_error
        assert self._pull_request is not None
        return self._pull_request

    async def get_pull_requests_by_head_refs(
        self,
        *,
        head_refs,
    ) -> dict[str, tuple[GithubPullRequest, ...]]:
        return self._branch_matches


def _lookup(
    github_client: _GithubClientStub,
    *,
    pull_request_number: int = 1,
) -> tuple[_OrphanedPullRequestInspection | None, CloseAction | None]:
    return asyncio.run(
        _lookup_orphaned_pull_request(
            cached_change=CachedChange(bookmark=_BOOKMARK),
            github_client=cast(GithubClient, github_client),
            pull_request_number=pull_request_number,
        )
    )


def test_lookup_orphaned_pr_blocks_when_saved_head_ref_no_longer_matches_bookmark() -> None:
    client = _GithubClientStub(pull_request=_pull_request(head_ref="review/some-other-branch"))

    inspection, blocked = _lookup(client)

    assert blocked is not None
    assert blocked.status == "blocked"
    assert "no longer has saved bookmark" in blocked.message
    assert _BOOKMARK in blocked.message
    assert inspection is not None
    assert inspection.state == "open"


def test_lookup_orphaned_pr_blocks_when_head_is_from_fork() -> None:
    client = _GithubClientStub(
        pull_request=_pull_request(head_label=f"fork-owner:{_BOOKMARK}"),
    )

    inspection, blocked = _lookup(client)

    assert blocked is not None
    assert blocked.status == "blocked"
    assert f"its head is fork-owner:{_BOOKMARK}" in blocked.message
    assert f"not {_OWNER}:{_BOOKMARK}" in blocked.message
    assert inspection is not None


def test_lookup_orphaned_pr_blocks_when_bookmark_has_multiple_live_pull_requests() -> None:
    saved_pr = _pull_request()
    client = _GithubClientStub(
        branch_matches={_BOOKMARK: (saved_pr, _pull_request(number=2))},
        pull_request=saved_pr,
    )

    inspection, blocked = _lookup(client)

    assert blocked is not None
    assert blocked.status == "blocked"
    assert "now has multiple pull requests" in blocked.message
    assert inspection is not None


def test_lookup_orphaned_pr_allows_close_when_saved_pr_is_the_only_branch_claimant() -> None:
    saved_pr = _pull_request()
    client = _GithubClientStub(
        branch_matches={_BOOKMARK: (saved_pr,)},
        pull_request=saved_pr,
    )

    inspection, blocked = _lookup(client)

    assert blocked is None
    assert inspection is not None
    assert inspection.state == "open"


def test_lookup_orphaned_pr_blocks_when_saved_pr_is_no_longer_on_github() -> None:
    client = _GithubClientStub(
        lookup_error=GithubClientError("GitHub request failed: 404 Not Found", status_code=404),
    )

    inspection, blocked = _lookup(client)

    assert blocked is not None
    assert blocked.status == "blocked"
    assert "PR #1 is no longer on GitHub" in blocked.message
    assert inspection is None
