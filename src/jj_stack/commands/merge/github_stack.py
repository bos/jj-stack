"""Asynchronous GitHub merge policy for one pull request or a stack prefix."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import jj_stack.ui as ui
from jj_stack.commands._github_stack_safety import selected_github_stack
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack, GithubStackMerge
from jj_stack.review.observation import RepositoryObservation
from jj_stack.ui import Message

from .models import MergeAction, MergeExecutionInputs, MergePlan, MergeResult, MergeRevision
from .preconditions import merge_precondition_error


@dataclass(frozen=True, slots=True)
class AsyncMergePlan:
    resource: GithubStack | None
    boundary_action: MergeAction | None
    planned: tuple[MergeRevision, ...]

    @property
    def terminal_retry(self) -> bool:
        return self.resource is not None and (
            self.target.identity.pr_number in self.resource.historical_pull_request_numbers
        )

    @property
    def target(self) -> MergeRevision:
        return self.planned[-1]

    def action(
        self,
        *,
        enqueued: bool = False,
        merge_action: str,
        method: str | None,
        trunk_branch: str,
    ) -> MergeAction:
        number_list = ", ".join(f"#{revision.identity.pr_number}" for revision in self.planned)
        pull_requests = f"PR {number_list}" if len(self.planned) == 1 else f"PRs {number_list}"
        if merge_action == "merge_queue" and enqueued:
            body = t"{pull_requests} are queued for {ui.bookmark(trunk_branch)} through "
        elif merge_action == "merge_queue":
            body = (
                t"add {pull_requests} to the merge queue for {ui.bookmark(trunk_branch)} through "
            )
        else:
            body = (
                t"merge {pull_requests} into {ui.bookmark(trunk_branch)} via "
                t"{ui.cmd(method or '')} through "
            )
        return MergeAction(
            kind="GitHub merge request",
            body=(body, t"commit {ui.commit_id(self.target.commit_id)}"),
            status="planned",
        )


def build_async_merge_plan(
    merge_plan: MergePlan,
    stacks: tuple[GithubStack, ...],
    target_change_id: str | None,
) -> AsyncMergePlan:
    by_pull = {
        revision.identity.pr_number: revision for revision in merge_plan.reviewed_revisions
    }
    resource = selected_github_stack(selected_pull_numbers=tuple(by_pull), stacks=stacks)
    if resource is None:
        if len(by_pull) > 1 and merge_plan.planned_revisions:
            raise CliError(
                "GitHub did not report a stack for this multi-PR review.",
                hint=t"Run {ui.cmd('submit')} before merging.",
            )
        return AsyncMergePlan(
            resource=None,
            boundary_action=merge_plan.boundary_action,
            planned=merge_plan.planned_revisions,
        )
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
        return AsyncMergePlan(
            resource,
            merge_plan.boundary_action,
            merge_plan.planned_revisions,
        )
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
        return AsyncMergePlan(resource, merge_plan.boundary_action, ())
    return AsyncMergePlan(resource, None, historical[:stop])


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
            await github.update_pull_request(
                pull_number=merge.target.identity.pr_number,
                base=execution.trunk_branch,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{merge.target.identity.pr_number} to "
                t"{ui.bookmark(execution.trunk_branch)}",
                hint="Resolve the GitHub error above, then rerun merge.",
            ) from error
    try:
        submission = await github.submit_stack_merge(
            expected_head_sha=merge.target.commit_id,
            merge_action=merge_action,
            merge_method=merge_method,
            pull_number=merge.target.identity.pr_number,
        )
    except GithubClientError as error:
        if error.status_code in {400, 409} and "head" in error.detail().casefold():
            return _blocked_result(
                execution,
                merge,
                reason=t"the PR head changed on GitHub; run "
                t"{ui.cmd(f'jj-stack submit {execution.selected_revset}')} and merge again",
            )
        raise CliError(
            t"Could not request GitHub merge through PR #{merge.target.identity.pr_number}.",
            hint="Resolve the GitHub error above, then rerun merge.",
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
                "a matching request is already pending; wait and rerun merge"
                if matching
                else "another GitHub stack merge request is already pending"
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
            reason=t"GitHub reports nothing merged: {reason}; if the stack conflicts with "
            t"{ui.bookmark(execution.trunk_branch)}, rebase onto {ui.revset('trunk()')}, resolve "
            t"the conflict, and run {submit} before merging again; if a check or repository rule "
            t"is failing, fix that on GitHub first",
        )
    if terminal.status == "enqueued":
        return _enqueued_result(
            execution,
            merge,
            merge_action=merge_action,
        )
    if terminal.status != "merged" or terminal.details.sha is None:
        raise CliError(
            "GitHub reported the stack merge as merged without a final trunk commit.",
            hint=t"Run {ui.cmd('jj-stack sync')} to reconcile whatever GitHub actually did.",
        )
    return _applied_result(
        execution,
        merge,
        final_sha=terminal.details.sha,
        merge_action=merge_action,
        merge_method=merge_method,
    )


def validate_terminal_retry(
    *,
    execution: MergeExecutionInputs,
    github: GithubClient,
    merge: AsyncMergePlan,
    observation: RepositoryObservation,
) -> None:
    """Validate a completed server operation from the observation that found it."""

    revisions = merge.planned
    error = merge_precondition_error(
        inactive_allowed=frozenset(revision.change_id for revision in revisions),
        expected_bases={},
        expected_repository=github.repository,
        expected_trunk_branch=execution.trunk_branch,
        observation=observation,
        remote_name=execution.remote_name,
        revisions=revisions,
    )
    if error:
        raise CliError(
            error,
            hint=t"Resolve the mismatch, then rerun {ui.cmd('jj-stack merge')}.",
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
    merge: AsyncMergePlan,
    *,
    reason: Message,
) -> MergeResult:
    location = (
        t"GitHub stack #{merge.resource.number}"
        if merge.resource is not None
        else t"PR #{merge.target.identity.pr_number}"
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
            trunk_branch=execution.trunk_branch,
        ),
        status="applied",
    )
    actions = (action, merge.boundary_action) if merge.boundary_action is not None else (action,)
    return execution.result(
        actions=actions,
        enqueued=True,
    )
