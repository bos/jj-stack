"""Observe a requested merge through completion, without storing operation progress."""

from __future__ import annotations

import asyncio
from math import ceil

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.identifiers import short_commit_id
from jj_stack.models.github import GithubPR, GithubStackMerge, GithubStackMergeDetails

from .plan import MergeChange, MergeExecutionInputs

_REQUEST_TIMEOUT_SECONDS = 600.0
_QUEUE_POLL_INTERVAL_SECONDS = 10.0
# GitHub drops a queue entry shortly before it records the merge or the removal reason, so an
# open, unqueued PR with no recorded reason is only a removal once it stays that way.
_UNEXPLAINED_POLLS = 6


async def wait_for_merge(
    github: GithubClient,
    result: GithubStackMerge,
    changes: tuple[MergeChange, ...],
    execution: MergeExecutionInputs,
) -> GithubStackMerge:
    console.output("Waiting for GitHub to merge. Ctrl-C stops waiting without cancelling.")
    try:
        if result.status == "pending":
            with console.spinner(
                description="GitHub is processing the merge request", report_changes=True
            ):
                result = await _request_result(github, result, changes[-1], execution)
        if result.status == "enqueued":
            result = await _queue_result(github, changes, execution)
    except asyncio.CancelledError, KeyboardInterrupt:
        console.warning(
            t"Stopped waiting; the GitHub merge request was not cancelled. "
            t"{execution.sync_after_github}"
        )
        raise
    except TimeoutError as error:
        raise CliError(
            "GitHub has not finished the merge request after 10 minutes; it may still complete.",
            hint=t"Rerun {execution.merge_command} to keep waiting, or run "
            t"{execution.sync_command} after GitHub finishes.",
        ) from error
    except GithubClientError as error:
        raise CliError(
            "Stopped waiting for GitHub's merge result; the request may still complete.",
            hint=execution.sync_after_github,
        ) from error
    return result


async def _request_result(
    github: GithubClient,
    result: GithubStackMerge,
    target: MergeChange,
    execution: MergeExecutionInputs,
) -> GithubStackMerge:
    operation_uuid = result.details.uuid
    if operation_uuid is None:
        raise CliError(
            "GitHub accepted the merge request but did not return an ID to poll.",
            hint=execution.sync_after_github,
        )
    poll_interval = 2.0
    async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
        while result.status == "pending":
            result = await github.poll_stack_merge(
                operation_uuid=operation_uuid, pr_number=target.identity.pr_number
            )
            if result.status == "pending":
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 30.0)
    return result


async def _queue_result(
    github: GithubClient,
    changes: tuple[MergeChange, ...],
    execution: MergeExecutionInputs,
) -> GithubStackMerge:
    numbers = [change.identity.pr_number for change in changes]
    unexplained: dict[int, int] = {}
    with console.spinner(
        description="Waiting in GitHub's merge queue", report_changes=True
    ) as progress:
        while True:
            observed = await github.get_prs_by_numbers(pr_numbers=numbers, merge_progress=True)
            prs = tuple(
                _matching_pr(observed.get(c.identity.pr_number), c, execution) for c in changes
            )
            progress.update("\n".join(_queue_progress(pr) for pr in prs))
            for pr in prs:
                if pr.state == "merged" or pr.is_queued:
                    unexplained.pop(pr.number, None)
                    continue
                unexplained[pr.number] = unexplained.get(pr.number, 0) + 1
                if _removal_reason(pr) is not None or unexplained[pr.number] > _UNEXPLAINED_POLLS:
                    raise _queue_removal(pr, prs, execution)
            if all(pr.state == "merged" for pr in prs):
                return GithubStackMerge(
                    status="merged", details=GithubStackMergeDetails(sha=prs[-1].merge_commit_sha)
                )
            await asyncio.sleep(_QUEUE_POLL_INTERVAL_SECONDS)


def _queue_removal(
    removed: GithubPR, prs: tuple[GithubPR, ...], execution: MergeExecutionInputs
) -> CliError:
    label = format_pr_number(removed.number, repo=execution.repo)
    reason = _removal_reason(removed)
    if reason is None:
        message: ui.Message = (
            t"PR {label} is {removed.state} but no longer in the merge queue, and GitHub has "
            t"not recorded why."
        )
    else:
        message = t"GitHub removed PR {label} from the merge queue: {reason.replace('_', ' ')}."
    hint: list[ui.Message] = []
    if removed.queue_test_commit is not None:
        url = (
            f"https://github.com/{execution.repo.full_name}/commit/"
            f"{removed.queue_test_commit}/checks"
        )
        hint.append(
            t"The queue ran its checks on its temporary merge commit "
            t"{ui.commit_id(short_commit_id(removed.queue_test_commit))}, not on the PR: "
            t"{ui.hyperlink(url, url)} "
        )
    merged = tuple(pr for pr in prs if pr.state == "merged")
    if merged:
        numbers = ui.join(lambda pr: format_pr_number(pr.number, repo=execution.repo), merged)
        hint.append(
            t"{'PR' if len(merged) == 1 else 'PRs'} {numbers} already merged. Run "
            t"{execution.sync_command} to update the local stack, then merge the remaining PRs."
        )
    elif reason is None:
        hint.append(
            t"Check the PR's timeline on GitHub for why it left the queue, then run "
            t"{execution.merge_command} again."
        )
    else:
        hint.append(
            t"Fix the reported issue on GitHub, then run {execution.merge_command} again."
        )
    return CliError(message, hint=tuple(hint))


def _removal_reason(pr: GithubPR) -> str | None:
    """GitHub's recorded reason for dropping the PR from the queue, unless it merged."""

    reason = pr.queue_removal_reason
    return None if reason == "merged" else reason


def _matching_pr(
    pr: GithubPR | None, change: MergeChange, execution: MergeExecutionInputs
) -> GithubPR:
    if (
        pr is None
        or pr.number != change.identity.pr_number
        or pr.head.ref != change.identity.head_ref
        or pr.head.sha != change.commit_id
    ):
        label = format_pr_number(change.identity.pr_number, repo=execution.repo)
        raise CliError(
            t"PR {label} changed or became unavailable while waiting.",
            hint=t"Inspect it on GitHub. If it merged, run {execution.sync_command}; otherwise "
            t"run {ui.cmd(f'jj-stack submit {execution.sync_head}')} and merge again.",
        )
    return pr


def _queue_progress(pr: GithubPR) -> str:
    entry = pr.merge_queue_entry
    if pr.state == "merged":
        return f"PR #{pr.number}: merged"
    if entry is None:
        return f"PR #{pr.number}: {pr.state} · left the queue; waiting for GitHub to record why"
    parts = [f"PR #{pr.number}: {(entry.state or 'QUEUED').lower().replace('_', ' ')}"]
    if entry.position is not None:
        total = f" of {entry.total}" if entry.total is not None else ""
        parts.append(f"queue position {entry.position}{total}")
    if entry.estimated_seconds is not None and entry.estimated_seconds >= 0:
        parts.append(
            f"GitHub estimates ~{max(1, ceil(entry.estimated_seconds / 60))} min to merge"
        )
    checks = entry.checks
    if checks is not None and checks.total and checks.remaining is not None:
        parts.append(f"{checks.remaining}/{checks.total} checks remaining")
        if checks.failed:
            parts.append(f"{checks.failed} failed")
    else:
        parts.append("waiting for queue checks to be reported")
    return " · ".join(parts)
