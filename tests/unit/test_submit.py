from __future__ import annotations

import pytest

from jj_review.commands.submit.command import (
    _ensure_pull_request_link_is_consistent,
    _ensure_remote_can_be_updated,
    _preflight_conflicted_revisions,
    _preflight_private_commits,
    _resolve_local_action,
)
from jj_review.errors import CliError
from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_review.models.github import GithubBranchRef, GithubPullRequest
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision
from tests.support.revision_helpers import make_revision


def test_resolve_local_action_rejects_conflicted_bookmark() -> None:
    with pytest.raises(
        CliError,
        match="2 conflicting local targets",
    ):
        _resolve_local_action("review/foo", ("abc123", "def456"), "abc123")


def test_ensure_remote_can_be_updated_rejects_conflicted_remote_bookmark() -> None:
    with pytest.raises(
        CliError,
        match="Remote bookmark review/foo@origin is conflicted",
    ):
        _ensure_remote_can_be_updated(
            bookmark="review/foo",
            bookmark_source="saved",
            bookmark_state=BookmarkState(name="review/foo"),
            change_id="change-a",
            desired_target="zzz999",
            remote="origin",
            remote_state=RemoteBookmarkState(
                remote="origin",
                targets=("abc123", "def456"),
                tracking_targets=("abc123", "def456"),
            ),
            state=ReviewState(changes={"change-a": CachedChange(bookmark="review/foo")}),
        )


def test_ensure_remote_can_be_updated_rejects_unproven_existing_remote_branch() -> None:
    with pytest.raises(
        CliError,
        match="already exists and points elsewhere",
    ):
        _ensure_remote_can_be_updated(
            bookmark="review/foo",
            bookmark_source="generated",
            bookmark_state=BookmarkState(name="review/foo"),
            change_id="change-a",
            desired_target="def456",
            remote="origin",
            remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
            state=ReviewState(),
        )


def test_ensure_remote_can_be_updated_allows_matching_untracked_remote_branch() -> None:
    _ensure_remote_can_be_updated(
        bookmark="review/foo",
        bookmark_source="generated",
        bookmark_state=BookmarkState(name="review/foo"),
        change_id="change-a",
        desired_target="abc123",
        remote="origin",
        remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
        state=ReviewState(),
    )


def test_pull_request_link_rejects_missing_discovered_pull_request() -> None:
    with pytest.raises(
        CliError,
        match="Saved pull request link exists",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            cached_change=CachedChange(
                bookmark="review/foo",
                pr_number=17,
                pr_url="https://github.test/octo-org/repo/pull/17",
            ),
            change_id="change-17",
            discovered_pull_request=None,
        )


def test_pull_request_link_rejects_mismatched_pull_request_number() -> None:
    with pytest.raises(
        CliError,
        match="Saved pull request #17 does not match",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            cached_change=CachedChange(bookmark="review/foo", pr_number=17),
            change_id="change-17",
            discovered_pull_request=_github_pull_request(number=21),
        )


class _FakeJjClientWithPrivateCommits:
    def __init__(self, private_revisions: tuple[LocalRevision, ...]) -> None:
        self._private_revisions = private_revisions

    def find_private_commits(
        self, revisions: tuple[LocalRevision, ...]
    ) -> tuple[LocalRevision, ...]:
        return self._private_revisions


def test_preflight_private_commits_passes_when_no_private_commits() -> None:
    client = _FakeJjClientWithPrivateCommits(())
    revisions = (
        make_revision(commit_id="head", change_id="head-change", description="feature\n"),
    )

    _preflight_private_commits(client, revisions)  # no exception


def test_preflight_private_commits_raises_on_private_commit() -> None:
    private = make_revision(
        commit_id="head", change_id="head-change", description="private thing\n"
    )
    client = _FakeJjClientWithPrivateCommits((private,))

    with pytest.raises(CliError, match="git.private-commits"):
        _preflight_private_commits(client, (private,))


def test_preflight_private_commits_error_names_the_blocked_changes() -> None:
    private = make_revision(
        commit_id="abc12345", change_id="abcd1234", description="secret work\n"
    )
    client = _FakeJjClientWithPrivateCommits((private,))

    with pytest.raises(CliError, match="secret work"):
        _preflight_private_commits(client, (private,))
def test_preflight_conflicted_revisions_raises_on_conflicted_change() -> None:
    conflicted = LocalRevision(
        change_id="head-change",
        commit_id="head",
        conflict=True,
        current_working_copy=False,
        description="conflicted feature\n",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("trunk",),
    )

    with pytest.raises(CliError, match="unresolved conflicts"):
        _preflight_conflicted_revisions((conflicted,))


def test_preflight_conflicted_revisions_error_names_the_blocked_changes() -> None:
    conflicted = LocalRevision(
        change_id="abcd1234",
        commit_id="abc12345",
        conflict=True,
        current_working_copy=False,
        description="conflicted feature\n",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("trunk",),
    )

    with pytest.raises(CliError, match="conflicted feature"):
        _preflight_conflicted_revisions((conflicted,))


def _github_pull_request(number: int) -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        body="",
        head=GithubBranchRef(ref="review/foo"),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        number=number,
        state="open",
        title="feature",
    )
