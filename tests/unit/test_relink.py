from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.commands.relink import _validated_relink_bookmark
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.stack import LocalRevision

_GITHUB_REPO = GithubRepoAddress(host="github.test", owner="octo-org", repo="stacked-review")
_REMOTE = GitRemote(name="origin", url="https://github.test/octo-org/stacked-review.git")


def _pull_request(*, head_label: str, state: str) -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(label="octo-org:main", ref="main"),
        head=GithubBranchRef(label=head_label, ref="review/manual-feature-1"),
        html_url="https://github.test/octo-org/stacked-review/pull/1",
        number=1,
        state=state,
        title="manual title",
    )


def _revision() -> LocalRevision:
    return LocalRevision(
        change_id="feature1change",
        commit_id="feature1commit",
        current_working_copy=False,
        description="feature 1\n",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("basecommit",),
    )


def test_validated_relink_bookmark_rejects_closed_pull_request() -> None:
    # The open-state guard fires before any JjClient call, so a bare client
    # (never invoked) is enough to exercise the rejection.
    pull_request = _pull_request(head_label="octo-org:review/manual-feature-1", state="closed")

    with pytest.raises(CliError, match="is not open"):
        _validated_relink_bookmark(
            client=JjClient(Path(".")),
            github_repository=_GITHUB_REPO,
            pull_request=pull_request,
            remote=_REMOTE,
            revision=_revision(),
        )


def test_validated_relink_bookmark_rejects_cross_repository_head() -> None:
    # A fork head label (owner != repo owner) is rejected before the bookmark
    # lookup, so the client is likewise never called.
    pull_request = _pull_request(head_label="someone-else:review/manual-feature-1", state="open")

    with pytest.raises(CliError, match="same-repository pull request branches"):
        _validated_relink_bookmark(
            client=JjClient(Path(".")),
            github_repository=_GITHUB_REPO,
            pull_request=pull_request,
            remote=_REMOTE,
            revision=_revision(),
        )
