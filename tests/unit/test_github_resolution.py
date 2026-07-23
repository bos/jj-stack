from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.github.resolution import (
    parse_github_repo,
    remote_bookmarks_pointing_at_commit,
    resolve_trunk_branch,
    select_submit_remote,
)
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.github import GithubRepository


def _remote(name: str) -> GitRemote:
    url = "git@example.com:org/repo.git"
    return GitRemote(name=name, fetch_url=url, push_url=url)


def test_select_submit_remote_uses_origin_when_multiple_remotes_exist() -> None:
    remote = select_submit_remote((_remote("origin"), _remote("backup")))

    assert remote.name == "origin"


@pytest.mark.parametrize(
    "remotes",
    [
        pytest.param(
            (_remote("backup"), _remote("upstream")),
            id="no-origin-among-many",
        ),
        pytest.param((), id="no-remotes"),
    ],
)
def test_select_submit_remote_rejects_remote_sets_without_a_determinable_remote(
    remotes: tuple[GitRemote, ...],
) -> None:
    with pytest.raises(
        CliError,
        match="Could not determine which Git remote to use",
    ):
        select_submit_remote(remotes)


def test_parse_github_repo_accepts_matching_fetch_and_push_urls() -> None:
    repository = parse_github_repo(
        GitRemote(
            name="origin",
            fetch_url="https://github.com/octo-org/stacked-review.git",
            push_url="ssh://git@ssh.github.com:443/octo-org/stacked-review.git",
        ),
    )

    assert repository is not None
    assert repository.host == "github.com"
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_parse_github_repo_parses_scp_style_remote_without_user() -> None:
    repository = parse_github_repo(
        GitRemote(
            name="origin",
            fetch_url="github.com:octo-org/stacked-review.git",
            push_url="github.com:octo-org/stacked-review.git",
        ),
    )

    assert repository is not None
    assert repository.host == "github.com"
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_parse_github_repo_returns_none_for_unparseable_remote() -> None:
    remote = GitRemote(
        name="origin",
        fetch_url="/tmp/remote.git",
        push_url="/tmp/remote.git",
    )

    assert parse_github_repo(remote) is None


def test_parse_github_repo_rejects_fetch_and_push_repository_mismatch() -> None:
    remote = GitRemote(
        name="origin",
        fetch_url="https://github.test/octo-org/stacked-review.git",
        push_url="git@github.test:octo-org/fork.git",
    )

    assert parse_github_repo(remote) is None


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
