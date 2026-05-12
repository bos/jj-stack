from __future__ import annotations

import pytest

from jj_review.errors import CliError
from jj_review.github.resolution import (
    parse_github_repo,
    remote_bookmarks_pointing_at_commit,
    resolve_trunk_branch,
    select_submit_remote,
)
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubRepository


def test_select_submit_remote_uses_origin_when_multiple_remotes_exist() -> None:
    remote = select_submit_remote(
        (
            GitRemote(name="origin", url="git@example.com:org/repo.git"),
            GitRemote(name="backup", url="git@example.com:org/repo.git"),
        ),
    )

    assert remote.name == "origin"


def test_select_submit_remote_rejects_ambiguous_remote_set_without_origin() -> None:
    with pytest.raises(
        CliError,
        match="Could not determine which Git remote to use",
    ):
        select_submit_remote(
            (
                GitRemote(name="backup", url="git@example.com:org/repo.git"),
                GitRemote(name="upstream", url="git@example.com:org/repo.git"),
            ),
        )


def test_select_submit_remote_rejects_empty_remote_list() -> None:
    with pytest.raises(
        CliError,
        match="Could not determine which Git remote to use",
    ):
        select_submit_remote(())


def test_parse_github_repo_parses_https_remote_url() -> None:
    repository = parse_github_repo(
        GitRemote(
            name="origin",
            url="https://github.test/octo-org/stacked-review.git",
        ),
    )

    assert repository is not None
    assert repository.host == "github.test"
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_parse_github_repo_parses_scp_style_remote_without_user() -> None:
    repository = parse_github_repo(
        GitRemote(
            name="origin",
            url="github.com:octo-org/stacked-review.git",
        ),
    )

    assert repository is not None
    assert repository.host == "github.com"
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_parse_github_repo_returns_none_for_unparseable_remote() -> None:
    assert parse_github_repo(GitRemote(name="origin", url="/tmp/remote.git")) is None


def test_resolve_trunk_branch_uses_repository_default_branch() -> None:
    branch = resolve_trunk_branch(
        bookmark_states={},
        github_repository_state=_github_repository(default_branch="main"),
        remote_name="origin",
        trunk_commit_id="trunk123",
    )

    assert branch == "main"


def test_resolve_trunk_branch_falls_back_to_unique_remote_bookmark() -> None:
    branch = resolve_trunk_branch(
        bookmark_states={
            "main": BookmarkState(
                name="main",
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("trunk123",)),),
            )
        },
        github_repository_state=_github_repository(default_branch=""),
        remote_name="origin",
        trunk_commit_id="trunk123",
    )

    assert branch == "main"


def test_resolve_trunk_branch_rejects_ambiguous_remote_bookmarks() -> None:
    with pytest.raises(
        CliError,
        match="multiple remote bookmarks",
    ):
        resolve_trunk_branch(
            bookmark_states={
                "main": BookmarkState(
                    name="main",
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=("trunk123",)),),
                ),
                "stable": BookmarkState(
                    name="stable",
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=("trunk123",)),),
                ),
            },
            github_repository_state=_github_repository(default_branch=""),
            remote_name="origin",
            trunk_commit_id="trunk123",
        )


def test_remote_bookmarks_pointing_at_commit_returns_sorted_matches() -> None:
    assert remote_bookmarks_pointing_at_commit(
        bookmark_states={
            "stable": BookmarkState(
                name="stable",
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("trunk123",)),),
            ),
            "main": BookmarkState(
                name="main",
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("trunk123",)),),
            ),
            "topic": BookmarkState(
                name="topic",
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("other456",)),),
            ),
        },
        remote_name="origin",
        commit_id="trunk123",
    ) == ("main", "stable")


def _github_repository(default_branch: str) -> GithubRepository:
    return GithubRepository(
        clone_url="https://github.test/octo-org/repo.git",
        default_branch=default_branch,
        full_name="octo-org/repo",
        html_url="https://github.test/octo-org/repo",
        name="repo",
        private=True,
        url="https://api.github.test/repos/octo-org/repo",
    )
