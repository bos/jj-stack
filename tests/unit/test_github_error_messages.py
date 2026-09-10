from __future__ import annotations

import pytest

from jj_stack.errors import EXIT_GITHUB, CliError, resolve_exit_code
from jj_stack.github.client import REPO_NOT_FOUND_REASON, GithubClientError
from jj_stack.github.error_messages import (
    repo_lookup_error,
    repo_lookup_reason,
    require_github_target,
)
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget, UnresolvedGithubTarget
from jj_stack.models.git import GitRemote
from jj_stack.ui import plain_text


def test_repo_lookup_treats_a_404_as_a_missing_or_hidden_repo() -> None:
    """Only a lookup of the repo itself may read a 404 as "no such repo for this token"."""

    missing = GithubClientError(
        "GitHub request failed: 404", body='{"message":"Not Found"}', status_code=404
    )
    refused = GithubClientError("GitHub request failed: 500", status_code=500)

    assert repo_lookup_reason(missing) == REPO_NOT_FOUND_REASON
    assert repo_lookup_reason(refused) == "request failed (GitHub 500)"

    wrapped = repo_lookup_error(missing, repo="octo-org/repo", hint="rerun later")
    assert plain_text(wrapped.message) == "Could not inspect GitHub repo octo-org/repo"
    assert wrapped.hint == REPO_NOT_FOUND_REASON
    assert repo_lookup_error(refused, repo="octo-org/repo", hint="rerun later").hint == (
        "rerun later"
    )
    try:
        raise wrapped from missing
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_GITHUB


def test_require_github_target_reports_the_earliest_resolution_failure() -> None:
    remote = GitRemote(name="origin", fetch_url="file:///repo", push_url="file:///repo")
    target = GithubTarget(remote=remote, repo=GithubRepoAddress(owner="octo-org", repo="repo"))

    assert require_github_target(target) is target
    with pytest.raises(CliError, match="No Git remote is configured"):
        require_github_target(UnresolvedGithubTarget())
    with pytest.raises(CliError, match="no usable remote"):
        require_github_target(UnresolvedGithubTarget(remote_error="no usable remote"))
    with pytest.raises(CliError, match="not a GitHub URL"):
        require_github_target(
            UnresolvedGithubTarget(remote=remote, github_repo_error="not a GitHub URL")
        )
