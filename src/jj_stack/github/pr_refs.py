"""Helpers for parsing GitHub pull request numbers and URLs."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import jj_stack.ui as ui
from jj_stack.errors import CliError, UsageError
from jj_stack.formatting import format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import CommitId
from jj_stack.models.github import GithubPR
from jj_stack.pr_branch_namespace import current_pr_branch_namespace

_PR_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$")


async def load_pr(*, github_client: GithubClient, pr_number: int) -> GithubPR:
    try:
        return await github_client.get_pr(pr_number=pr_number)
    except GithubClientError as error:
        pr_label = format_pr_number(pr_number, repo=github_client.repo)
        raise CliError(t"Could not load pull request {pr_label}") from error


def pr_head_repo_problem(pr: GithubPR, repo: GithubRepoAddress) -> ui.Message | None:
    """Explain why a PR's head branch does not belong to the selected repo."""

    pr_number_label = format_pr_number(pr.number, url=pr.html_url)
    if pr.head.label != f"{repo.owner}:{pr.head.ref}":
        return (
            t"Pull request {pr_number_label}'s head branch "
            t"{ui.bookmark(pr.head.label or pr.head.ref)} does not belong to {repo.full_name}."
        )
    return None


def managed_pr_head_problem(pr: GithubPR, repo: GithubRepoAddress) -> ui.Message | None:
    """Explain why a PR's head is not owned by this repo and branch namespace, if it is not."""

    if (problem := pr_head_repo_problem(pr, repo)) is not None:
        return problem
    namespace = current_pr_branch_namespace()
    pr_number_label = format_pr_number(pr.number, url=pr.html_url)
    if not namespace.contains(pr.head.ref):
        return (
            t"Pull request {pr_number_label}'s head branch "
            t"{ui.bookmark(pr.head.ref)} is not a jj-stack PR branch; its name does not start "
            t"with {ui.bookmark(namespace.branch_prefix)}."
        )
    return None


def require_managed_pr_head(*, pr: GithubPR, repo: GithubRepoAddress) -> CommitId:
    """Return the head commit of a PR owned by this repo and branch namespace."""

    if (problem := managed_pr_head_problem(pr, repo)) is not None:
        raise CliError(problem)
    return pr.head.sha


def parse_pr_number(reference: str) -> int | None:
    if reference.isdigit():
        return int(reference)
    return None


def parse_repo_pr_reference(
    *,
    github_repo: GithubRepoAddress,
    reference: str,
) -> int:
    parsed = parse_pr_number(reference)
    if parsed is not None:
        return parsed

    url = urlparse(reference)
    match = (
        _PR_URL_RE.fullmatch(url.path)
        if url.scheme in {"http", "https"} and url.hostname
        else None
    )
    if match is None:
        raise UsageError(
            f"Pull request reference {reference} is not a pull request number "
            f"or URL for {github_repo.full_name}."
        )
    if (match["owner"], match["repo"]) != (github_repo.owner, github_repo.repo):
        raise UsageError(
            f"Pull request URL {reference} does not match configured repo "
            f"{github_repo.full_name}."
        )
    return int(match["number"])
