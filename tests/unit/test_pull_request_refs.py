from __future__ import annotations

import pytest

from jj_review.errors import CliError
from jj_review.github.pull_request_refs import (
    ParsedPullRequestUrl,
    parse_pull_request_url,
    parse_repository_pull_request_reference,
)
from jj_review.github.resolution import ParsedGithubRepo


def test_parse_pull_request_url_accepts_standard_pull_request_url() -> None:
    assert parse_pull_request_url("https://github.test/octo-org/stacked-review/pull/17") == (
        ParsedPullRequestUrl(
            host="github.test",
            number=17,
            owner="octo-org",
            repo="stacked-review",
        )
    )


def test_parse_pull_request_url_rejects_non_pull_request_urls() -> None:
    assert parse_pull_request_url("https://github.test/octo-org/stacked-review/issues/17") is None


def test_parse_repository_pull_request_reference_accepts_matching_url() -> None:
    assert (
        parse_repository_pull_request_reference(
            reference="https://github.test/octo-org/stacked-review/pull/17",
            github_repository=ParsedGithubRepo(
                host="github.test",
                owner="octo-org",
                repo="stacked-review",
            ),
            invalid_reference_message="invalid",
        )
        == 17
    )


def test_parse_repository_pull_request_reference_rejects_wrong_repository() -> None:
    with pytest.raises(CliError, match="does not match configured repository"):
        parse_repository_pull_request_reference(
            reference="https://github.test/other-org/stacked-review/pull/17",
            github_repository=ParsedGithubRepo(
                host="github.test",
                owner="octo-org",
                repo="stacked-review",
            ),
            invalid_reference_message="invalid",
        )
