"""Observe the required PR checks before asking GitHub to merge or enqueue them."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_number
from jj_stack.github.client import GithubClient
from jj_stack.github.error_messages import read_or_stop
from jj_stack.models.github import GithubPR
from jj_stack.models.github_details import GithubCheck, GithubPRMergeDetails

from .plan import MergeChange

_CHECK_POLL_INTERVAL_SECONDS = 10.0
_PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_FAILED = {
    "FAILURE",
    "ERROR",
    "ACTION_REQUIRED",
    "CANCELLED",
    "TIMED_OUT",
    "STALE",
    "STARTUP_FAILURE",
}


@dataclass(frozen=True, slots=True)
class ChecksPending:
    changes: tuple[MergeChange, ...]
    progress: tuple[str, ...]

    async def wait(self) -> None:
        """Wait without holding the repo lock; the caller must replan before mutating."""

        with console.spinner(description="Waiting for required checks"):
            await asyncio.sleep(_CHECK_POLL_INTERVAL_SECONDS)


async def inspect_required_checks(
    github: GithubClient, changes: tuple[MergeChange, ...], prs: tuple[GithubPR, ...]
) -> ChecksPending | None:
    # Once queued, GitHub tests temporary merge-group commits and owns the queue decision.
    unqueued = tuple(pr for pr in prs if not pr.is_queued)
    if not unqueued:
        return None
    details = await read_or_stop(
        github.get_pr_merge_details(prs=unqueued),
        message="Could not inspect required checks before merging.",
        hint="Resolve the GitHub error, then rerun jj-stack merge.",
    )
    pending: list[str] = []
    for pr in unqueued:
        evidence = details.get(pr.number)
        if evidence is None:
            raise CliError(
                t"PR {format_pr_number(pr.number, url=pr.html_url)} changed while inspecting "
                t"required checks.",
                hint="Inspect the stack with jj-stack view, then rerun jj-stack merge.",
            )
        pending.extend(_pending_checks(pr, evidence))
    return ChecksPending(changes, tuple(pending)) if pending else None


def _pending_checks(pr: GithubPR, evidence: GithubPRMergeDetails) -> tuple[str, ...]:
    # A test-merge result takes precedence over the head's result for the same required name.
    merge_names = {check.name for check in evidence.merge_checks}
    checks = tuple(check for check in evidence.checks if check.name not in merge_names)
    checks += evidence.merge_checks
    required = tuple(check for check in checks if check.is_required)
    for check in required:
        if check.state in _FAILED:
            raise _failed_check(pr, check)
    pending = tuple(check.name for check in required if check.state not in _PASSED)
    return tuple(f"PR #{pr.number}: {name}" for name in (*pending, *evidence.missing_checks))


def _failed_check(pr: GithubPR, check: GithubCheck) -> CliError:
    url = check.url or f"{pr.html_url}/checks"
    return CliError(
        t"Required check {check.name} on PR {format_pr_number(pr.number, url=pr.html_url)}: "
        t"{check.state.lower().replace('_', ' ')}. {ui.hyperlink(url, url)}",
        hint="Fix or rerun the required check, then rerun jj-stack merge.",
    )
