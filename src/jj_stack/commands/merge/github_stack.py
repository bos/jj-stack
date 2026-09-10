"""Asynchronous GitHub merge policy for one pull request or a stack prefix."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label, format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack, GithubStackMerge
from jj_stack.stack.github_stack_safety import selected_github_stack
from jj_stack.ui import Message

from .models import MergeAction, MergeChange, MergeExecutionInputs, MergePlan, MergeResult

_MERGE_POLL_TIMEOUT_SECONDS = 600.0
_MAX_MERGE_POLL_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class AsyncMergePlan:
    resource: GithubStack | None
    boundary_action: MergeAction | None
    planned: tuple[MergeChange, ...]

    @property
    def target(self) -> MergeChange:
        return self.planned[-1]

    def action(
        self,
        *,
        enqueued: bool = False,
        merge_action: str,
        method: str | None,
        repo: GithubRepoAddress,
        trunk_branch: str,
    ) -> MergeAction:
        numbers = ui.join(
            lambda change: format_pr_number(change.identity.pr_number, repo=repo),
            self.planned,
        )
        prs: Message = ("PR " if len(self.planned) == 1 else "PRs ", numbers)
        if merge_action == "merge_queue" and enqueued:
            body = t"queued {prs} for {ui.bookmark(trunk_branch)} through "
        elif merge_action == "merge_queue":
            body = t"add {prs} to the merge queue for {ui.bookmark(trunk_branch)} through "
        else:
            body = (
                t"merge {prs} into {ui.bookmark(trunk_branch)} via {ui.cmd(method or '')} up to "
            )
        return MergeAction(
            kind="GitHub merge request",
            body=(
                body,
                t"change {ui.change_id(self.target.change_id)} "
                t"(commit ID {ui.commit_id(self.target.commit_id)})",
            ),
            status="planned",
        )


def build_async_merge_plan(
    merge_plan: MergePlan,
    stacks: tuple[GithubStack, ...],
    execution: MergeExecutionInputs,
) -> AsyncMergePlan:
    pr_numbers = {change.identity.pr_number for change in merge_plan.linked_changes}
    resource = selected_github_stack(execution.repo, pr_numbers, stacks)
    if resource is None:
        if len(pr_numbers) > 1 and merge_plan.planned_changes:
            raise CliError(
                "GitHub did not report a stack for these pull requests.",
                hint=t"Run {ui.cmd('jj-stack submit')} before merging.",
            )
        return AsyncMergePlan(
            resource=None,
            boundary_action=merge_plan.boundary_action,
            planned=merge_plan.planned_changes,
        )
    if merge_plan.planned_changes:
        planned_numbers = tuple(
            change.identity.pr_number for change in merge_plan.planned_changes
        )
        if resource.active_pr_numbers[: len(planned_numbers)] != planned_numbers:
            raise CliError(
                t"GitHub stack #{resource.number} does not start with the pull requests at the "
                t"bottom of the local stack.",
                hint=t"Run {ui.cmd('jj-stack submit')} so GitHub's stack matches the local "
                t"stack, then retry.",
            )
        return AsyncMergePlan(
            resource,
            merge_plan.boundary_action,
            merge_plan.planned_changes,
        )
    return AsyncMergePlan(resource, merge_plan.boundary_action, ())


async def execute_async_merge(
    *,
    execution: MergeExecutionInputs,
    github: GithubClient,
    merge_action: str,
    merge_method: str | None,
    merge: AsyncMergePlan,
) -> MergeResult:
    if not merge.planned:
        return execution.result(
            actions=(() if merge.boundary_action is None else (merge.boundary_action,))
        )
    if merge.resource is None and merge.target.base_ref != execution.trunk_branch:
        try:
            await github.update_pr(
                pr_number=merge.target.identity.pr_number,
                base=execution.trunk_branch,
            )
        except GithubClientError as error:
            pr_label = format_pr_label(
                merge.target.identity.pr_number,
                repo=github.repo,
            )
            raise CliError(
                t"Could not retarget {pr_label} to {ui.bookmark(execution.trunk_branch)}",
                hint="Resolve the GitHub error above, then rerun jj-stack merge.",
            ) from error
    try:
        submission = await github.submit_stack_merge(
            expected_head_sha=merge.target.commit_id,
            merge_action=merge_action,
            merge_method=merge_method,
            pr_number=merge.target.identity.pr_number,
        )
    except GithubClientError as error:
        if error.status_code in {400, 409} and "head" in error.github_message().casefold():
            return _blocked_result(
                execution,
                merge,
                reason=t"the PR head changed on GitHub; run "
                t"{ui.cmd(f'jj-stack submit {execution.selected_revset}')} and merge again",
            )
        pr_label = format_pr_label(
            merge.target.identity.pr_number,
            repo=github.repo,
        )
        raise CliError(
            t"Could not request GitHub merge through {pr_label}.",
            hint="Resolve the GitHub error above, then rerun jj-stack merge.",
        ) from error
    if submission.already_pending:
        details = submission.result.details
        matching = (
            details.expected_head_sha == merge.target.commit_id
            and details.merge_action == merge_action
            and details.merge_method == merge_method
        )
        return _blocked_result(
            execution,
            merge,
            reason=(
                "a matching merge request is already pending; wait for GitHub to finish, "
                "then run jj-stack sync if it merged"
                if matching
                else "another merge request is already pending; check its status on GitHub "
                "and run jj-stack sync if it merges"
            ),
        )
    terminal = await _terminal(
        github,
        submission.result,
        merge.target.identity.pr_number,
    )
    if terminal.status == "failed":
        reason = terminal.details.message or "GitHub did not provide a failure reason"
        submit = ui.cmd(f"jj-stack submit {execution.selected_revset}")
        return _blocked_result(
            execution,
            merge,
            reason=t"GitHub rejected the merge: {reason}. If the stack conflicts with "
            t"{ui.bookmark(execution.trunk_branch)}, rebase onto {ui.revset('trunk()')}, resolve "
            t"the conflicts, and run {submit} before merging again. For a failed check or "
            t"unmet repo requirement, fix the issue reported by GitHub first",
        )
    if terminal.status == "enqueued":
        return _enqueued_result(
            execution,
            merge,
            merge_action=merge_action,
        )
    if terminal.status != "merged" or terminal.details.sha is None:
        raise CliError(
            "GitHub reported the stack merge as complete but did not say which trunk commit it "
            "produced.",
            hint=t"Check the PRs on GitHub, then run {ui.cmd('jj-stack sync')} for this stack "
            t"to apply any completed merges.",
        )
    return _applied_result(
        execution,
        merge,
        final_sha=terminal.details.sha,
        merge_action=merge_action,
        merge_method=merge_method,
    )


async def _terminal(
    github: GithubClient,
    result: GithubStackMerge,
    pr_number: int,
) -> GithubStackMerge:
    operation_uuid = result.details.uuid
    if result.status == "pending" and operation_uuid is None:
        raise CliError(
            "GitHub accepted the merge request, but jj-stack cannot check its progress.",
            hint=t"Check the pull request on GitHub. After it merges, run "
            t"{ui.cmd('jj-stack sync')} for this stack.",
        )
    poll_interval = 2.0
    try:
        async with asyncio.timeout(_MERGE_POLL_TIMEOUT_SECONDS):
            while result.status == "pending":
                result = await github.poll_stack_merge(
                    operation_uuid=operation_uuid or "",
                    pr_number=pr_number,
                )
                if result.status == "pending":
                    await asyncio.sleep(poll_interval)
                    poll_interval = min(
                        poll_interval * 1.5,
                        _MAX_MERGE_POLL_INTERVAL_SECONDS,
                    )
    except TimeoutError as error:
        raise CliError(
            "GitHub's merge request is still pending after 10 minutes.",
            hint=t"The request may still complete on GitHub. Do not rerun "
            t"{ui.cmd('jj-stack merge')} while it is "
            t"pending; check the pull request on GitHub, then run "
            t"{ui.cmd('jj-stack sync')} if it merges.",
        ) from error
    return result


def _blocked_result(
    execution: MergeExecutionInputs,
    merge: AsyncMergePlan,
    *,
    reason: Message,
) -> MergeResult:
    location = (
        t"GitHub stack #{merge.resource.number}"
        if merge.resource is not None
        else format_pr_label(
            merge.target.identity.pr_number,
            repo=execution.repo,
        )
    )
    return execution.result(
        actions=(
            MergeAction(
                kind="boundary",
                body=t"at {location}: {reason}",
                status="blocked",
            ),
        )
    )


def _applied_result(
    execution: MergeExecutionInputs,
    merge: AsyncMergePlan,
    *,
    final_sha: str,
    merge_action: str,
    merge_method: str | None,
) -> MergeResult:
    action = replace(
        merge.action(
            merge_action=merge_action,
            method=merge_method,
            repo=execution.repo,
            trunk_branch=execution.trunk_branch,
        ),
        status="applied",
    )
    actions = (action, merge.boundary_action) if merge.boundary_action is not None else (action,)
    return execution.result(
        actions=actions,
        final_trunk_commit_id=final_sha,
    )


def _enqueued_result(
    execution: MergeExecutionInputs,
    merge: AsyncMergePlan,
    *,
    merge_action: str,
) -> MergeResult:
    action = replace(
        merge.action(
            enqueued=True,
            merge_action=merge_action,
            method=None,
            repo=execution.repo,
            trunk_branch=execution.trunk_branch,
        ),
        status="applied",
    )
    actions = (action, merge.boundary_action) if merge.boundary_action is not None else (action,)
    return execution.result(
        actions=actions,
        enqueued=True,
    )
