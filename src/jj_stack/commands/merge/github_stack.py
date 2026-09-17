"""Asynchronous GitHub merge policy for one pull request or a stack prefix."""

from __future__ import annotations

from dataclasses import dataclass, replace

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label, format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack, GithubStackMerge
from jj_stack.stack.github_stack_safety import selected_github_stack
from jj_stack.ui import Message

from .plan import MergeAction, MergeChange, MergeExecutionInputs, MergePlan, MergeResult
from .wait import wait_for_merge


@dataclass(frozen=True, slots=True)
class AsyncMergePlan:
    resource: GithubStack | None
    boundary_action: MergeAction | None
    planned: tuple[MergeChange, ...]

    @property
    def target(self) -> MergeChange:
        return self.planned[-1]

    def actions(self, action: MergeAction | None = None) -> tuple[MergeAction, ...]:
        return tuple(item for item in (action, self.boundary_action) if item is not None)

    def action(
        self,
        *,
        outcome: str = "planned",
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
        if outcome == "merged":
            body = t"merged {prs} into {ui.bookmark(trunk_branch)} through "
        elif outcome == "enqueued":
            body = t"queued {prs} for {ui.bookmark(trunk_branch)} through "
        elif merge_action == "merge_queue":
            verb = "asked GitHub to add" if outcome == "pending" else "add"
            body = t"{verb} {prs} to the merge queue for {ui.bookmark(trunk_branch)} through "
        elif outcome == "pending":
            body = t"requested merge of {prs} into {ui.bookmark(trunk_branch)} through "
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


@dataclass(frozen=True, slots=True)
class PendingMerge:
    """A merge request GitHub accepted but has not finished."""

    execution: MergeExecutionInputs
    merge: AsyncMergePlan
    merge_action: str
    merge_method: str | None
    request: GithubStackMerge

    def result(self) -> MergeResult:
        """Report the request as still pending."""

        return _accepted_result(
            self.execution,
            self.merge,
            result=self.request,
            merge_action=self.merge_action,
            merge_method=self.merge_method,
        )

    async def wait(self, github: GithubClient) -> MergeResult:
        """Wait for GitHub to finish the request, then report its outcome."""

        terminal = await wait_for_merge(github, self.request, self.merge.planned, self.execution)
        return _terminal_result(
            self.execution,
            self.merge,
            terminal,
            merge_action=self.merge_action,
            merge_method=self.merge_method,
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
    elif merge_plan.planned_changes:
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
    return AsyncMergePlan(resource, merge_plan.boundary_action, merge_plan.planned_changes)


async def execute_async_merge(
    *,
    execution: MergeExecutionInputs,
    github: GithubClient,
    merge_action: str,
    merge_method: str | None,
    merge: AsyncMergePlan,
) -> MergeResult | PendingMerge:
    """Ask GitHub to merge; report the outcome, or the request GitHub is still working on."""

    if not merge.planned:
        return MergeResult(actions=merge.actions())
    pr_label = format_pr_label(merge.target.identity.pr_number, repo=github.repo)
    if merge.resource is None and merge.target.base_ref != execution.trunk_branch:
        try:
            await github.update_pr(
                pr_number=merge.target.identity.pr_number,
                base=execution.trunk_branch,
            )
        except GithubClientError as error:
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
                t"{ui.cmd(f'jj-stack submit {execution.selected_head}')} and merge again",
            )
        raise CliError(
            t"Could not request GitHub merge through {pr_label}.",
            hint="Resolve the GitHub error above, then rerun jj-stack merge.",
        ) from error
    # A queue chooses its own method, so the pending request's method is not part of its identity.
    details = submission.result.details
    if submission.already_pending and not (
        details.expected_head_sha == merge.target.commit_id
        and details.merge_action == merge_action
        and (merge_action == "merge_queue" or details.merge_method == merge_method)
    ):
        return _blocked_result(
            execution,
            merge,
            reason="another merge request is already pending; check its status on GitHub "
            "and run jj-stack sync if it merges",
        )
    request = submission.result
    if request.status in {"pending", "enqueued"}:
        return PendingMerge(
            execution=execution,
            merge=merge,
            merge_action=merge_action,
            merge_method=merge_method,
            request=request,
        )
    return _terminal_result(
        execution, merge, request, merge_action=merge_action, merge_method=merge_method
    )


def _terminal_result(
    execution: MergeExecutionInputs,
    merge: AsyncMergePlan,
    terminal: GithubStackMerge,
    *,
    merge_action: str,
    merge_method: str | None,
) -> MergeResult:
    if terminal.status == "failed":
        return _blocked_result(
            execution,
            merge,
            reason=_rejection_reason(execution, terminal.details.message),
        )
    if terminal.status == "merged" and terminal.details.sha is None:
        raise CliError(
            "GitHub reported the stack merge as complete but did not say which trunk commit it "
            "produced.",
            hint=t"Check the PRs on GitHub, then run {ui.cmd('jj-stack sync')} for this stack "
            t"to apply any completed merges.",
        )
    return _accepted_result(
        execution,
        merge,
        result=terminal,
        merge_action=merge_action,
        merge_method=merge_method,
    )


def _rejection_reason(execution: MergeExecutionInputs, message: str | None) -> Message:
    reason = (message or "GitHub did not provide a failure reason").strip()
    punctuation = "" if reason.endswith((".", "!", "?")) else "."
    if "conflict" in reason.casefold():
        hint = (
            t"Rebase onto {ui.revset('trunk()')}, resolve the conflicts, and run "
            t"{ui.cmd(f'jj-stack submit {execution.selected_head}')} before merging again."
        )
    else:
        hint = t"Fix the reported issue on GitHub, then run {execution.merge_command} again."
    return t"GitHub rejected the merge: {reason}{punctuation} {hint}"


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
    return MergeResult(
        actions=(
            MergeAction(
                kind="boundary",
                body=t"at {location}: {reason}",
                status="blocked",
            ),
        )
    )


def _accepted_result(
    execution: MergeExecutionInputs,
    merge: AsyncMergePlan,
    *,
    result: GithubStackMerge,
    merge_action: str,
    merge_method: str | None,
) -> MergeResult:
    action = replace(
        merge.action(
            outcome=result.status,
            merge_action=merge_action,
            method=merge_method,
            repo=execution.repo,
            trunk_branch=execution.trunk_branch,
        ),
        status="applied",
    )
    return MergeResult(
        actions=merge.actions(action),
        final_trunk_commit_id=result.details.sha,
        pending=result.status if result.status in {"pending", "enqueued"} else None,
    )
