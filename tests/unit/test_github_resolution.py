from __future__ import annotations

from typing import cast

import pytest

from jj_stack.errors import CliError
from jj_stack.github.resolution import (
    parse_github_repo,
    resolve_trunk_branch,
    select_submit_remote,
)
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubRepository
from jj_stack.review_namespace import ReviewNamespace

_NAMESPACE = ReviewNamespace("jj-stack")


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
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_parse_github_repo_returns_none_for_unparseable_remote() -> None:
    remote = GitRemote(
        name="origin",
        fetch_url="/tmp/remote.git",
        push_url="/tmp/remote.git",
    )

    assert parse_github_repo(remote) is None


def test_parse_github_repo_accepts_distinct_ssh_host_aliases() -> None:
    repository = parse_github_repo(
        GitRemote(
            name="origin",
            fetch_url="git@gh-bos:octo-org/stacked-review.git",
            push_url="git@gh-voxel:octo-org/stacked-review.git",
        )
    )

    assert repository is not None
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_parse_github_repo_rejects_fetch_and_push_repository_mismatch() -> None:
    remote = GitRemote(
        name="origin",
        fetch_url="https://github.com/octo-org/stacked-review.git",
        push_url="git@github.com:octo-org/fork.git",
    )

    assert parse_github_repo(remote) is None


def test_resolve_trunk_branch_prefers_the_default_branch_when_it_is_one_of_the_matches() -> None:
    client = _RemoteBranchClient({"main": "trunk123", "stable": "trunk123"})

    branch, targets = resolve_trunk_branch(
        client=cast(JjClient, client),
        github_repository_state=_github_repository(default_branch="main"),
        namespace=_NAMESPACE,
        remote=_remote("origin"),
        trunk_commit_id="trunk123",
    )

    assert branch == "main"
    assert targets == {"main": "trunk123", "stable": "trunk123"}
    assert client.patterns == [("refs/heads/*",)]


def test_resolve_trunk_branch_accepts_a_default_branch_ahead_of_local_trunk() -> None:
    client = _RemoteBranchClient({"main": "moved-ahead"})

    branch, _targets = resolve_trunk_branch(
        client=cast(JjClient, client),
        github_repository_state=_github_repository(default_branch="main"),
        namespace=_NAMESPACE,
        remote=_remote("origin"),
        trunk_commit_id="stale-local-trunk",
    )

    assert branch == "main"


def test_resolve_trunk_branch_rejects_a_default_branch_that_is_not_trunk() -> None:
    client = _RemoteBranchClient({"develop": "develop999", "main": "trunk123"})

    with pytest.raises(CliError, match="default branch"):
        resolve_trunk_branch(
            client=cast(JjClient, client),
            github_repository_state=_github_repository(default_branch="develop"),
            namespace=_NAMESPACE,
            remote=_remote("origin"),
            trunk_commit_id="trunk123",
        )


def test_resolve_trunk_branch_falls_back_to_unique_non_review_remote_branch() -> None:
    client = _RemoteBranchClient(
        {
            "main": "trunk123",
            "jj-stack/feature-abcdefgh": "trunk123",
        }
    )

    branch, targets = resolve_trunk_branch(
        client=cast(JjClient, client),
        github_repository_state=_github_repository(default_branch=""),
        namespace=_NAMESPACE,
        remote=_remote("origin"),
        trunk_commit_id="trunk123",
    )

    assert branch == "main"
    assert targets == {"main": "trunk123"}


def test_resolve_trunk_branch_rejects_ambiguous_remote_branches() -> None:
    with pytest.raises(
        CliError,
        match="multiple remote branches",
    ):
        resolve_trunk_branch(
            client=cast(
                JjClient,
                _RemoteBranchClient({"main": "trunk123", "stable": "trunk123"}),
            ),
            github_repository_state=_github_repository(default_branch=""),
            namespace=_NAMESPACE,
            remote=_remote("origin"),
            trunk_commit_id="trunk123",
        )


class _RemoteBranchClient:
    def __init__(self, targets: dict[str, str]) -> None:
        self.targets = targets
        self.patterns: list[tuple[str, ...]] = []

    def list_remote_branches(
        self,
        *,
        remote: str,
        patterns: tuple[str, ...],
    ) -> dict[str, str]:
        assert remote == "origin"
        self.patterns.append(patterns)
        if patterns == ("refs/heads/*",):
            return dict(self.targets)
        requested = {pattern.removeprefix("refs/heads/") for pattern in patterns}
        return {branch: target for branch, target in self.targets.items() if branch in requested}


def _github_repository(default_branch: str) -> GithubRepository:
    return GithubRepository(
        default_branch=default_branch,
        full_name="octo-org/repo",
    )
