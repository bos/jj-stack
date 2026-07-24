"""Native asynchronous merge policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubAsyncMerge, GithubStack
from jj_stack.review.observation import observe_review_mutation
from jj_stack.ui import Message

from .authority import merge_authority_error
from .models import MergeAction, MergeExecutionInputs, MergePlan, MergeResult, MergeRevision


@dataclass(frozen=True, slots=True)
class NativeMergePlan:
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
            kind="native stack request",
            body=t"PUT one {ui.cmd(method)} native prefix for PRs {numbers} through "
            t"commit {ui.commit_id(self.target.commit_id)}",
            status="planned",
        )


def build_native_merge_plan(
    merge_plan: MergePlan,
    stacks: tuple[GithubStack, ...],
    supported: bool,
    target_change_id: str | None,
) -> NativeMergePlan | None:
    if not supported:
        return None
    by_pull = {
        revision.identity.pr_number: revision for revision in merge_plan.reviewed_revisions
    }
    overlaps = tuple(
        stack for stack in stacks if not set(by_pull).isdisjoint(stack.pull_request_numbers)
    )
    if not overlaps:
        if len(by_pull) > 1:
            raise CliError(
                "GitHub did not report a native stack for this multi-PR review.",
                hint=t"Run {ui.cmd('submit')} before merging.",
            )
        return None
    if len(overlaps) != 1:
        raise CliError("Selected reviews overlap multiple native GitHub stacks.")
    resource = overlaps[0]
    try:
        active = tuple(by_pull[number] for number in resource.active_pull_request_numbers)
    except KeyError as error:
        raise CliError(
            t"GitHub stack #{resource.number} has an unselected active member."
        ) from error
    if merge_plan.planned_revisions:
        planned_numbers = tuple(
            revision.identity.pr_number for revision in merge_plan.planned_revisions
        )
        if resource.active_pull_request_numbers[: len(planned_numbers)] != planned_numbers:
            raise CliError(
                t"GitHub stack #{resource.number} does not match the candidate prefix."
            )
        return NativeMergePlan(resource, active, merge_plan.planned_revisions)
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
    return NativeMergePlan(resource, active, historical[:stop], terminal_retry=True)


async def execute_native_merge(
    *,
    execution: MergeExecutionInputs,
    github: GithubClient,
    merge_method: str,
    native: NativeMergePlan,
) -> MergeResult:
    await authorize_native_merge(execution, github, native)
    try:
        submission = await github.submit_stack_merge(
            expected_head_sha=native.target.commit_id,
            merge_method=merge_method,
            pull_number=native.target.identity.pr_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not request native merge through PR #{native.target.identity.pr_number}."
        ) from error
    if submission.conflict:
        details = submission.result.details
        matching = (
            details.expected_head_sha == native.target.commit_id
            and details.merge_method == merge_method
        )
        return _result(
            execution,
            native,
            reason=(
                "a matching request is already pending; wait and rerun merge"
                if matching
                else "another native merge request is already pending"
            ),
        )
    terminal = await _terminal(github, submission.result, native.target.identity.pr_number)
    if terminal.status == "failed":
        reason = terminal.details.message or "GitHub did not provide a failure reason"
        return _result(execution, native, reason=t"GitHub reports nothing merged: {reason}")
    if terminal.status != "merged" or terminal.details.sha is None:
        raise CliError("GitHub async merge completed without a final trunk commit.")
    return _result(
        execution,
        native,
        final_sha=terminal.details.sha,
        merge_method=merge_method,
    )


async def authorize_native_merge(
    execution: MergeExecutionInputs,
    github: GithubClient,
    native: NativeMergePlan,
) -> None:
    resource = await github.get_stack(stack_number=native.resource.number)
    revisions = native.planned if native.terminal_retry else native.active
    inactive = revisions if native.terminal_retry else native.active[len(native.planned) :]
    observation = await observe_review_mutation(
        change_ids=tuple(revision.change_id for revision in revisions),
        context=execution.context,
        github_client=github,
        remote_name=execution.remote_name,
        trunk_branch=execution.trunk_branch,
    )
    error = merge_authority_error(
        inactive_allowed=frozenset(revision.change_id for revision in inactive),
        expected_bases={revision.change_id: (revision.base_ref,) for revision in revisions},
        expected_repository=github.repository,
        expected_trunk_branch=execution.trunk_branch,
        expected_trunk_commit_id=execution.trunk_commit_id,
        observation=observation,
        remote_name=execution.remote_name,
        revisions=revisions,
    )
    if error or resource.pull_request_numbers != native.resource.pull_request_numbers:
        raise CliError(error or t"GitHub stack #{resource.number} changed after planning.")


async def _terminal(
    github: GithubClient,
    result: GithubAsyncMerge,
    pull_number: int,
) -> GithubAsyncMerge:
    operation_uuid = result.details.uuid
    if result.status == "pending" and operation_uuid is None:
        raise CliError("GitHub accepted an async merge without an operation UUID.")
    while result.status == "pending":
        result = await github.poll_stack_merge(
            operation_uuid=operation_uuid or "",
            pull_number=pull_number,
        )
        if result.status == "pending":
            await asyncio.sleep(1)
    return result


def _result(
    execution: MergeExecutionInputs,
    native: NativeMergePlan,
    *,
    final_sha: str | None = None,
    merge_method: str | None = None,
    reason: Message | None = None,
) -> MergeResult:
    if reason is not None:
        return execution.result(
            actions=(
                MergeAction(
                    kind="boundary",
                    body=t"at native stack #{native.resource.number}: {reason}",
                    status="blocked",
                ),
            )
        )
    return execution.result(
        actions=(replace(native.action(merge_method or ""), status="applied"),),
        final_trunk_commit_id=final_sha,
        merged_change_ids=tuple(revision.change_id for revision in native.planned),
    )
