"""Check repo setup and GitHub access.

Checks the Git remote, authentication, GitHub access, your permission to push to the repo, stacked
pull request support, and GitHub's default branch. It also reports PR bookmarks imported by a
fetch and leftovers from an interrupted checkout or sync.

Run `jj-stack doctor --fix` to configure fetches to skip PR branches, forget untracked PR
bookmarks imported by a fetch, and remove checkout or sync leftovers. These repairs affect only
this local repo.

The command exits 1 if a check fails and 0 otherwise. Warnings and problems repaired by `--fix`
do not count as failures. The report includes recovery commands where available.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError, error_message
from jj_stack.github.auth import github_token, github_token_from_env
from jj_stack.github.client import (
    GithubClient,
    GithubClientError,
    build_github_client,
)
from jj_stack.github.resolution import (
    GithubRepoAddress,
    parse_github_repo,
    select_submit_remote,
)
from jj_stack.github.stack_availability import github_stacks_unavailable_error
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubRepo
from jj_stack.pr_branch_namespace import current_pr_branch_namespace
from jj_stack.state.operation_lock import operation_lock
from jj_stack.ui import Message

HELP = "Check repo setup and GitHub connectivity"

type CheckDetail = Message


@dataclass(slots=True, frozen=True)
class CheckResult:
    label: str
    status: Literal["ok", "warn", "fail", "fixed", "skip"]
    detail: CheckDetail


# The checks that need the GitHub API, in report order. The later ones are skipped when the
# repo cannot be reached.
_GITHUB_CHECKS = ("connectivity", "push access", "GitHub stacks", "trunk branch")


def doctor(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    fix: bool,
    repo: Path | None,
) -> int:
    """CLI entrypoint for `doctor`."""
    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    with (
        operation_lock(
            context.state_store,
            command="doctor --fix",
            mutating=fix,
        ),
        console.spinner(description="Running checks"),
    ):
        results = asyncio.run(_run_checks(context=context, fix=fix))
    console.output(_results_table(results))
    return 1 if any(r.status == "fail" for r in results) else 0


async def _run_checks(
    *,
    context: CommandContext,
    fix: bool,
) -> list[CheckResult]:
    results: list[CheckResult] = []

    # Check 1: Git remote selection
    remote_result, selected_remote = _check_git_remote(context=context)
    results.append(remote_result)

    if selected_remote is None:
        results.extend(
            _skipped(
                "PR branch fetch",
                "PR bookmarks",
                "checkout/sync leftovers",
                "GitHub remote",
                "GitHub auth",
                *_GITHUB_CHECKS,
            )
        )
        return results

    results.append(
        _check_pr_branch_fetch_isolation(context=context, fix=fix, remote=selected_remote)
    )
    results.append(_check_pr_bookmarks(context=context, fix=fix))
    results.append(_check_pr_branch_temp(context=context, fix=fix))

    # Check 2: GitHub remote parsing
    github_result, parsed_repo = _check_github_remote(selected_remote)
    results.append(github_result)

    if parsed_repo is None:
        results.extend(_skipped("GitHub auth", *_GITHUB_CHECKS))
        return results

    # Check 3: GitHub auth
    auth_result, token = _check_github_auth()
    results.append(auth_result)

    if token is None:
        results.extend(_skipped(*_GITHUB_CHECKS))
        return results

    # Checks 4-7: connectivity, push access, Stacks API availability, and trunk branch
    results.extend(await _check_github_access(parsed_repo=parsed_repo))
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


def _check_github_remote(remote: GitRemote) -> tuple[CheckResult, GithubRepoAddress | None]:
    parsed = parse_github_repo(remote)
    if parsed is None:
        return (
            CheckResult(
                "GitHub remote",
                "fail",
                t"remote {ui.bookmark(remote.name)} does not have fetch and push URLs "
                t"for the same GitHub repo; use GitHub HTTPS or SSH URLs",
            ),
            None,
        )
    return CheckResult("GitHub remote", "ok", parsed.full_name), parsed


def _check_pr_branch_fetch_isolation(
    *,
    context: CommandContext,
    fix: bool,
    remote: GitRemote,
) -> CheckResult:
    namespace = current_pr_branch_namespace()
    try:
        isolation = context.jj_client.ensure_pr_branch_fetch_isolation(
            remote=remote.name,
            dry_run=not fix,
        )
    except CliError as error:
        detail: CheckDetail = error_message(error)
        if error.hint is not None:
            detail = (detail, t" {error.hint}")
        return CheckResult("PR branch fetch", "warn", detail)
    if isolation.status == "required":
        if isolation.problem == "missing":
            problem_detail = (
                t"{ui.cmd('jj git fetch')} does not skip {ui.bookmark(namespace.branch_glob)} "
                t"branches; fix with {ui.cmd('jj-stack doctor --fix')}.",
            )
        else:
            problem_detail = (
                t"the fetch rule that skips {ui.bookmark(namespace.branch_glob)} branches is "
                t"duplicated; keep one with {ui.cmd('jj-stack doctor --fix')}.",
            )
        return CheckResult("PR branch fetch", "warn", problem_detail)
    return CheckResult(
        "PR branch fetch",
        "fixed" if isolation.status == "applied" else "ok",
        t"{ui.cmd('jj git fetch')} skips {ui.bookmark(namespace.branch_glob)} branches",
    )


def _check_pr_bookmarks(*, context: CommandContext, fix: bool) -> CheckResult:
    """Report visible PR bookmarks; with --fix, forget the ones a fetch imported.

    Untracked remote bookmarks make their commits immutable for the user's own jj commands, so a
    clone made before the fetch exclusion existed cannot edit an adopted stack until they go.
    """

    client = context.jj_client
    imported = client.untracked_pr_bookmarks()
    visible = tuple(name for name in client.visible_pr_bookmark_targets() if name not in imported)
    remaining: CheckDetail = (
        t"; visible bookmarks remain: {ui.join(ui.bookmark, visible)}" if visible else ""
    )
    if imported and fix:
        client.forget_bookmarks(imported)
        return CheckResult(
            "PR bookmarks",
            "fixed",
            (t"forgot {ui.join(ui.bookmark, imported)}", remaining),
        )
    if imported:
        pronoun = "it" if len(imported) == 1 else "them"
        return CheckResult(
            "PR bookmarks",
            "warn",
            (
                t"{ui.join(ui.bookmark, imported)} came from a fetch and "
                t"{'makes its commit' if len(imported) == 1 else 'make their commits'} "
                t"immutable for jj; forget {pronoun} with {ui.cmd('jj-stack doctor --fix')}",
                remaining,
            ),
        )
    if visible:
        return CheckResult(
            "PR bookmarks",
            "warn",
            t"visible bookmarks remain: {ui.join(ui.bookmark, visible)}; check them with "
            t"{ui.cmd('jj bookmark list --all-remotes')}",
        )
    return CheckResult("PR bookmarks", "ok", "none")


def _check_pr_branch_temp(*, context: CommandContext, fix: bool) -> CheckResult:
    artifacts = context.jj_client.pr_branch_temp_artifacts()
    if artifacts.ref_target is None and not artifacts.bookmark_targets:
        return CheckResult("checkout/sync leftovers", "ok", "none")
    if fix:
        context.jj_client.clear_pr_branch_temp_artifacts()
        return CheckResult("checkout/sync leftovers", "fixed", "removed")
    return CheckResult(
        "checkout/sync leftovers",
        "warn",
        t"leftovers from an interrupted checkout or sync remain; remove them with "
        t"{ui.cmd('jj-stack doctor --fix')} or rerun the interrupted command",
    )


def _check_github_auth() -> tuple[CheckResult, str | None]:
    env_token = github_token_from_env()
    if env_token:
        env_var = "GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN") else "GH_TOKEN"
        return CheckResult("GitHub auth", "ok", f"token found ({env_var})"), env_token

    # Env vars not set — try the gh CLI
    token = github_token()
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


async def _check_github_access(*, parsed_repo: GithubRepoAddress) -> list[CheckResult]:
    """Run the checks that need the GitHub API, sharing one client."""

    async with build_github_client(repo=parsed_repo) as client:
        try:
            github_repo = await client.get_repo()
        except GithubClientError as error:
            reason = error.user_facing_reason()
        except Exception as error:
            reason = f"request failed ({error})"
        else:
            return [
                CheckResult("connectivity", "ok", f"reached {parsed_repo.full_name}"),
                _check_push_access(github_repo),
                await _check_github_stacks(client, parsed_repo),
                _check_trunk_branch(github_repo),
            ]
    return [
        CheckResult("connectivity", "fail", f"{parsed_repo.full_name}: {reason}"),
        *(CheckResult(label, "skip", "connectivity failed") for label in _GITHUB_CHECKS[1:]),
    ]


def _check_push_access(github_repo: GithubRepo) -> CheckResult:
    """Report whether the token can push PR branches to the repo that receives the PRs.

    A clone of a repo the user cannot push to, such as an upstream they have only forked, cannot
    hold PR branches, and GitHub cannot stack PRs whose branches live in a fork.
    """

    permissions = github_repo.permissions
    if permissions is None:
        return CheckResult(
            "push access",
            "warn",
            f"GitHub did not report your permissions for {github_repo.full_name}",
        )
    if permissions.push:
        return CheckResult("push access", "ok", f"can push to {github_repo.full_name}")
    return CheckResult(
        "push access",
        "fail",
        f"no push access to {github_repo.full_name}; jj-stack pushes PR branches to the repo "
        f"that receives the PRs, and GitHub stacks cannot span forks. Ask for write access to "
        f"this repo.",
    )


async def _check_github_stacks(
    client: GithubClient,
    parsed_repo: GithubRepoAddress,
) -> CheckResult:
    try:
        await client.list_stacks()
    except GithubClientError as error:
        unavailable = github_stacks_unavailable_error(
            error=error,
            repo=parsed_repo.full_name,
        )
        detail: CheckDetail = (
            (unavailable.message, t" {unavailable.hint}")
            if unavailable is not None
            else f"could not inspect stacks: {error.user_facing_reason()}"
        )
        return CheckResult("GitHub stacks", "fail", detail)
    return CheckResult("GitHub stacks", "ok", "stacked pull requests available")


def _check_trunk_branch(github_repo: GithubRepo) -> CheckResult:
    if github_repo.default_branch:
        return CheckResult("trunk branch", "ok", github_repo.default_branch)
    return CheckResult(
        "trunk branch",
        "warn",
        t"GitHub repo has no default branch set; choose a default branch in the repo's "
        t"GitHub settings",
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
