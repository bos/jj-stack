"""Helpers for parsing GitHub pull request numbers and URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from jj_stack.errors import UsageError
from jj_stack.github.resolution import GithubRepoAddress

_PR_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$")


@dataclass(frozen=True, slots=True)
class ParsedPRUrl:
    number: int
    owner: str
    repo: str


def parse_pr_number(reference: str) -> int | None:
    if reference.isdigit():
        return int(reference)
    return None


def parse_pr_url(reference: str) -> ParsedPRUrl | None:
    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    match = _PR_URL_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return ParsedPRUrl(
        number=int(match.group("number")),
        owner=match.group("owner"),
        repo=match.group("repo"),
    )


def parse_repo_pr_reference(
    *,
    github_repo: GithubRepoAddress,
    invalid_reference_message: str | None = None,
    reference: str,
    wrong_repo_message: str | None = None,
) -> int:
    parsed = parse_pr_number(reference)
    if parsed is not None:
        return parsed

    pr_url = parse_pr_url(reference)
    if pr_url is None:
        raise UsageError(
            invalid_reference_message
            or (
                f"Pull request reference {reference} is not a pull request number "
                f"or URL for {github_repo.full_name}."
            )
        )
    if pr_url.owner != github_repo.owner or pr_url.repo != github_repo.repo:
        raise UsageError(
            wrong_repo_message
            or (
                f"Pull request URL {reference} does not match configured repo "
                f"{github_repo.full_name}."
            )
        )
    return pr_url.number
