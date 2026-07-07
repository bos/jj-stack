"""Helpers for parsing GitHub pull request numbers and URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from jj_stack.errors import UsageError
from jj_stack.github.resolution import GithubRepoAddress

_PULL_REQUEST_URL_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$"
)


@dataclass(frozen=True, slots=True)
class ParsedPullRequestUrl:
    host: str
    number: int
    owner: str
    repo: str


def parse_pull_request_number(reference: str) -> int | None:
    if reference.isdigit():
        return int(reference)
    return None


def parse_pull_request_url(reference: str) -> ParsedPullRequestUrl | None:
    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    match = _PULL_REQUEST_URL_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return ParsedPullRequestUrl(
        host=parsed.hostname,
        number=int(match.group("number")),
        owner=match.group("owner"),
        repo=match.group("repo"),
    )


def parse_repository_pull_request_reference(
    *,
    github_repository: GithubRepoAddress,
    invalid_reference_message: str | None = None,
    reference: str,
    wrong_host_message: str | None = None,
    wrong_repository_message: str | None = None,
) -> int:
    parsed = parse_pull_request_number(reference)
    if parsed is not None:
        return parsed

    pull_request_url = parse_pull_request_url(reference)
    if pull_request_url is None:
        raise UsageError(
            invalid_reference_message
            or (
                f"Pull request reference {reference} is not a pull request number "
                f"or URL for {github_repository.full_name}."
            )
        )
    if pull_request_url.host != github_repository.host:
        raise UsageError(
            wrong_host_message
            or (
                f"Pull request URL {reference} does not match configured host "
                f"{github_repository.host}."
            )
        )
    if (
        pull_request_url.owner != github_repository.owner
        or pull_request_url.repo != github_repository.repo
    ):
        raise UsageError(
            wrong_repository_message
            or (
                f"Pull request URL {reference} does not match configured repository "
                f"{github_repository.full_name}."
            )
        )
    return pull_request_url.number
