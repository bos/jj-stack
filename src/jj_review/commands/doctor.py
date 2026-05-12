"""Check jj-review's configuration and connectivity.

Runs a series of read-only checks and prints a status line for each. Nothing
is changed. Exit status is 0 if all checks pass or warn; 1 if any check fails.

It checks remote resolution, GitHub repository discovery, GitHub token
availability, GitHub API access, and trunk discovery.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.errors import CliError, error_message
from jj_review.github.auth import github_token_for_base_url, github_token_from_env
from jj_review.github.client import (
    GithubClient,
    GithubClientError,
)
from jj_review.github.error_messages import summarize_github_error_reason
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    select_submit_remote,
)
from jj_review.jj import JjCliArgs
from jj_review.models.bookmarks import GitRemote
from jj_review.models.github import GithubRepository
from jj_review.ui import Message

HELP = "Check GitHub auth, remote resolution, and local state"

type CheckDetail = Message


@dataclass(slots=True, frozen=True)
class CheckResult:
    label: str
    status: Literal["ok", "warn", "fail", "skip"]
    detail: CheckDetail


def doctor(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `doctor`."""
    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with console.spinner(description="Running checks"):
        results = asyncio.run(_run_checks(context=context))
    console.output(_results_table(results))
    return 1 if any(r.status == "fail" for r in results) else 0


async def _run_checks(
    *,
    context: CommandContext,
) -> list[CheckResult]:
    results: list[CheckResult] = []

    # Check 1: Git remote selection
    remote_result, selected_remote = _check_git_remote(context=context)
    results.append(remote_result)

    if selected_remote is None:
        results.extend(_skipped("GitHub remote", "GitHub auth", "connectivity", "trunk branch"))
        return results

    # Check 2: GitHub remote parsing
    github_result, parsed_repo = _check_github_remote(selected_remote)
    results.append(github_result)

    if parsed_repo is None:
        results.extend(_skipped("GitHub auth", "connectivity", "trunk branch"))
        return results

    # Check 3: GitHub auth
    auth_result, token = _check_github_auth(parsed_repo.api_base_url)
    results.append(auth_result)

    if token is None:
        results.extend(_skipped("connectivity", "trunk branch"))
        return results

    # Checks 4 & 5: Connectivity and trunk branch
    connectivity_result, github_repo = await _check_github_connectivity(
        parsed_repo=parsed_repo,
        token=token,
    )
    results.append(connectivity_result)

    if github_repo is not None:
        results.append(_check_trunk_branch(github_repo))
    else:
        results.append(CheckResult("trunk branch", "skip", "connectivity failed"))

    return results


def _skipped(*labels: str) -> list[CheckResult]:
    return [CheckResult(label, "skip", "prior check failed") for label in labels]


def _check_git_remote(*, context: CommandContext) -> tuple[CheckResult, GitRemote | None]:
    jj_client = context.jj_client
    try:
        remotes = jj_client.list_git_remotes()
    except Exception as error:
        return CheckResult("remote", "fail", f"could not list remotes: {error}"), None

    if not remotes:
        return (
            CheckResult(
                "remote",
                "fail",
                t"no Git remotes configured; run {ui.cmd('jj git remote add origin <url>')} "
                t"to add one",
            ),
            None,
        )

    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        return CheckResult("remote", "fail", error_message(error)), None

    return CheckResult("remote", "ok", ui.bookmark(remote.name)), remote


def _check_github_remote(remote: GitRemote) -> tuple[CheckResult, ParsedGithubRepo | None]:
    parsed = parse_github_repo(remote)
    if parsed is None:
        return (
            CheckResult(
                "GitHub remote",
                "fail",
                t"remote {ui.bookmark(remote.name)} does not look like "
                t"a GitHub URL: {remote.url}; use a GitHub HTTPS or SSH remote URL",
            ),
            None,
        )
    return CheckResult("GitHub remote", "ok", f"{parsed.host}/{parsed.full_name}"), parsed


def _check_github_auth(base_url: str) -> tuple[CheckResult, str | None]:
    env_token = github_token_from_env()
    if env_token:
        env_var = "GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN") else "GH_TOKEN"
        return CheckResult("GitHub auth", "ok", f"token found ({env_var})"), env_token

    # Env vars not set — try the gh CLI
    token = github_token_for_base_url(base_url)
    if token:
        return CheckResult("GitHub auth", "ok", "token found (gh CLI)"), token

    return (
        CheckResult(
            "GitHub auth",
            "fail",
            t"no token found; set GITHUB_TOKEN or run {ui.cmd('gh auth login')}",
        ),
        None,
    )


async def _check_github_connectivity(
    *,
    parsed_repo: ParsedGithubRepo,
    token: str,
) -> tuple[CheckResult, GithubRepository | None]:
    # Use the token already resolved by the auth check rather than re-invoking
    # the gh CLI. GithubClient is a module-level name so tests can patch it.
    async with GithubClient(base_url=parsed_repo.api_base_url, token=token) as client:
        try:
            github_repo = await client.get_repository(parsed_repo.owner, parsed_repo.repo)
        except GithubClientError as error:
            return (
                CheckResult(
                    "connectivity",
                    "fail",
                    f"{parsed_repo.host}/{parsed_repo.full_name}: "
                    f"{summarize_github_error_reason(error)}",
                ),
                None,
            )
        except Exception as error:
            return (
                CheckResult(
                    "connectivity",
                    "fail",
                    f"{parsed_repo.host}/{parsed_repo.full_name}: request failed ({error})",
                ),
                None,
            )
    return (
        CheckResult(
            "connectivity",
            "ok",
            f"reached {parsed_repo.host}/{parsed_repo.full_name}",
        ),
        github_repo,
    )


def _check_trunk_branch(github_repo: GithubRepository) -> CheckResult:
    if github_repo.default_branch:
        return CheckResult("trunk branch", "ok", github_repo.default_branch)
    return CheckResult(
        "trunk branch",
        "warn",
        t"GitHub repository has no default branch set; set a default branch on GitHub "
        t"or configure {ui.revset('trunk()')} in jj",
    )


def _results_table(results: list[CheckResult]) -> ui.DataTable:
    return ui.DataTable(
        columns=(
            ui.TableColumn("check"),
            ui.TableColumn("status", no_wrap=True),
            ui.TableColumn("detail"),
        ),
        rows=tuple(
            (
                result.label,
                ui.status(result.status),
                result.detail,
            )
            for result in results
        ),
    )
