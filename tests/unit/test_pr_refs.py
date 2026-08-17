from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.github.pr_refs import (
    ParsedPRUrl,
    parse_pr_url,
    parse_repo_pr_reference,
)
from jj_stack.github.resolution import GithubRepoAddress


def test_parse_pr_url_ignores_hostname() -> None:
    assert parse_pr_url("https://pr.example/octo-org/stacked-prs/pull/17") == (
        ParsedPRUrl(
            number=17,
            owner="octo-org",
            repo="stacked-prs",
        )
    )


def test_parse_pr_url_rejects_non_pr_urls() -> None:
    assert parse_pr_url("https://github.com/octo-org/stacked-prs/issues/17") is None


def test_parse_repo_pr_reference_accepts_matching_url() -> None:
    assert (
        parse_repo_pr_reference(
            reference="https://github.com/octo-org/stacked-prs/pull/17",
            github_repo=GithubRepoAddress(
                owner="octo-org",
                repo="stacked-prs",
            ),
            invalid_reference_message="invalid",
        )
        == 17
    )


def test_parse_repo_pr_reference_rejects_wrong_repo() -> None:
    with pytest.raises(CliError, match="does not match configured repo"):
        parse_repo_pr_reference(
            reference="https://github.com/other-org/stacked-prs/pull/17",
            github_repo=GithubRepoAddress(
                owner="octo-org",
                repo="stacked-prs",
            ),
            invalid_reference_message="invalid",
        )
