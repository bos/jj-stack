"""GitHub stack merge policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import jj_stack.ui as ui
from jj_stack.commands._github_stack_safety import selected_github_stack
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack, GithubStackMerge
from jj_stack.review.observation import observe_reviews
from jj_stack.ui import Message

from .models import MergeAction, MergeExecutionInputs, MergePlan, MergeResult, MergeRevision
from .preconditions import merge_precondition_error


@dataclass(frozen=True, slots=True)
class GithubStackMergePlan:
    resource: GithubStack
    active: tuple[MergeRevision, ...]
    planned: tuple[MergeRevision, ...]
    terminal_retry: bool = False

    @property
    def target(self) -> MergeRevision:
        return self.planned[-1]

    def action(self, method: str) -> MergeAction:
        numbers = ", ".join(f"#{revision.identity.pr_number}" for revision in self.planned)
        return MergeAction(
            kind="GitHub stack merge request",
            body=t"PUT one {ui.cmd(method)} GitHub stack prefix for PRs {numbers} through "
            t"commit {ui.commit_id(self.target.commit_id)}",
            status="planned",
        )


def build_github_stack_merge_plan(
    merge_plan: MergePlan,
    stacks: tuple[GithubStack, ...],
    target_change_id: str | None,
) -> GithubStackMergePlan | None:
    by_pull = {
        revision.identity.pr_number: revision for revision in merge_plan.reviewed_revisions
    }
    resource = selected_github_stack(selected_pull_numbers=tuple(by_pull), stacks=stacks)
    if resource is None:
        if len(by_pull) > 1:
            raise CliError(
                "GitHub did not report a stack for this multi-PR review.",
                hint=t"Run {ui.cmd('submit')} before merging.",
            )
        return None
    active = tuple(by_pull[number] for number in resource.active_pull_request_numbers)
    if merge_plan.planned_revisions:
        planned_numbers = tuple(
            revision.identity.pr_number for revision in merge_plan.planned_revisions
        )
        if resource.active_pull_request_numbers[: len(planned_numbers)] != planned_numbers:
            raise CliError(
                t"GitHub stack #{resource.number} does not match the candidate prefix.",
                hint=t"Run {ui.cmd('jj-stack submit')} so the stack matches this path, "
                t"then retry.",
            )
        return GithubStackMergePlan(resource, active, merge_plan.planned_revisions)
    historical = tuple(
        by_pull[number]
        for number in resource.historical_pull_request_numbers
        if number in by_pull
    )
    change_ids = tuple(revision.change_id for revision in historical)
    stop = len(historical) if target_change_id is None else 0
    if target_change_id in change_ids:
        stop = change_ids.index(target_change_id) + 1
    if not stop or merge_plan.reviewed_revisions[: len(historical)] != historical:
        return None
    return GithubStackMergePlan(resource, active, historical[:stop], terminal_retry=True)


async def execute_github_stack_merge(
    *,
    execution: MergeExecutionInputs,
    github: GithubClient,
    merge_method: str,
    github_stack_merge: GithubStackMergePlan,
) -> MergeResult:
    await check_github_stack_merge(execution, github, github_stack_merge)
    try:
        submission = await github.submit_stack_merge(
            expected_head_sha=github_stack_merge.target.commit_id,
            merge_method=merge_method,
            pull_number=github_stack_merge.target.identity.pr_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not request GitHub stack merge through "
            t"PR #{github_stack_merge.target.identity.pr_number}.",
            hint="Resolve the GitHub error above, then rerun merge.",
        ) from error
    if submission.already_pending:
        details = submission.result.details
        matching = (
            details.expected_head_sha == github_stack_merge.target.commit_id
            and details.merge_method == merge_method
        )
        return _blocked_result(
            execution,
            github_stack_merge,
            reason=(
                "a matching request is already pending; wait and rerun merge"
                if matching
                else "another GitHub stack merge request is already pending"
            ),
        )
    terminal = await _terminal(
        github,
        submission.result,
        github_stack_merge.target.identity.pr_number,
    )
    if terminal.status == "failed":
        reason = terminal.details.message or "GitHub did not provide a failure reason"
        submit = ui.cmd(f"jj-stack submit {execution.selected_revset}")
        return _blocked_result(
            execution,
            github_stack_merge,
            reason=t"GitHub reports nothing merged: {reason}; if the stack conflicts with "
            t"{ui.bookmark(execution.trunk_branch)}, rebase onto {ui.revset('trunk()')}, resolve "
            t"the conflict, and run {submit} before merging again; if a check or repository rule "
            t"is failing, fix that on GitHub first",
        )
    if terminal.status != "merged" or terminal.details.sha is None:
        raise CliError(
            "GitHub reported the stack merge as merged without a final trunk commit.",
            hint=t"Run {ui.cmd('jj-stack sync')} to reconcile whatever GitHub actually did.",
        )
    return _applied_result(
        execution,
        github_stack_merge,
        final_sha=terminal.details.sha,
        merge_method=merge_method,
    )


async def check_github_stack_merge(
    execution: MergeExecutionInputs,
    github: GithubClient,
    github_stack_merge: GithubStackMergePlan,
) -> None:
    resource = await github.get_stack(stack_number=github_stack_merge.resource.number)
    revisions = (
        github_stack_merge.planned
        if github_stack_merge.terminal_retry
        else github_stack_merge.active
    )
    inactive = (
        revisions
        if github_stack_merge.terminal_retry
        else github_stack_merge.active[len(github_stack_merge.planned) :]
    )
    observation = await observe_reviews(
        change_ids=tuple(revision.change_id for revision in revisions),
        context=execution.context,
        github_client=github,
        remote_name=execution.remote_name,
        trunk_branch=execution.trunk_branch,
    )
    error = merge_precondition_error(
        inactive_allowed=frozenset(revision.change_id for revision in inactive),
        expected_bases={revision.change_id: (revision.base_ref,) for revision in revisions},
        expected_repository=github.repository,
        expected_trunk_branch=execution.trunk_branch,
        observation=observation,
        remote_name=execution.remote_name,
        revisions=revisions,
    )
    if error or resource.pull_request_numbers != github_stack_merge.resource.pull_request_numbers:
        raise CliError(
            error or t"GitHub stack #{resource.number} changed after planning.",
            hint=t"Rerun {ui.cmd('jj-stack merge')} to plan against the current stack.",
        )


async def _terminal(
    github: GithubClient,
    result: GithubStackMerge,
    pull_number: int,
) -> GithubStackMerge:
    operation_uuid = result.details.uuid
    if result.status == "pending" and operation_uuid is None:
        raise CliError(
            "GitHub accepted the stack merge without an operation ID to follow.",
            hint=t"Run {ui.cmd('jj-stack sync')} to see whether the merge completed.",
        )
    while result.status == "pending":
        result = await github.poll_stack_merge(
            operation_uuid=operation_uuid or "",
            pull_number=pull_number,
        )
        if result.status == "pending":
            await asyncio.sleep(1)
    return result


def _blocked_result(
    execution: MergeExecutionInputs,
    github_stack_merge: GithubStackMergePlan,
    *,
    reason: Message,
) -> MergeResult:
    return execution.result(
        actions=(
            MergeAction(
                kind="boundary",
                body=t"at GitHub stack #{github_stack_merge.resource.number}: {reason}",
                status="blocked",
            ),
        )
    )


def _applied_result(
    execution: MergeExecutionInputs,
    github_stack_merge: GithubStackMergePlan,
    *,
    final_sha: str,
    merge_method: str,
) -> MergeResult:
    return execution.result(
        actions=(replace(github_stack_merge.action(merge_method), status="applied"),),
        final_trunk_commit_id=final_sha,
        merged_change_ids=tuple(revision.change_id for revision in github_stack_merge.planned),
    )
