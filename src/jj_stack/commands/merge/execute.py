"""Apply ordinary pull-request merges for one selected review path.

Each mutation is authorized from fresh local, remote, and GitHub observations. GitHub moves
trunk; this command does not rewrite local history or retire review tracking. A later selected
`sync` observes and reconciles whatever GitHub accepted.
"""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.jj.client import JjCommandError

from .models import (
    MergeAction,
    MergeExecutionInputs,
    MergePlan,
    MergeResult,
)
from .pull_requests import merge_pull_request


async def execute_merge_plan(
    *,
    github_client: GithubClient,
    merge_method: str,
    plan: MergePlan,
    execution: MergeExecutionInputs,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_commit_id: str,
    trunk_subject: str,
) -> MergeResult:
    """Merge the planned bottom prefix through GitHub's ordinary PR API."""

    def merge_result(
        *,
        actions: tuple[MergeAction, ...],
        applied: bool,
        blocked: bool,
        merged_change_ids: tuple[str, ...] = (),
    ) -> MergeResult:
        return MergeResult(
            actions=actions,
            applied=applied,
            blocked=blocked,
            merged_change_ids=merged_change_ids,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
        )

    if plan.blocked:
        return merge_result(
            actions=plan.planned_actions(),
            applied=False,
            blocked=True,
        )

    execution.context.state_store.require_writable()
    await execution.native_stacks.require_unstacked()

    actions: list[MergeAction] = []
    merged_change_ids: list[str] = []
    blocked_action: MergeAction | None = None
    current_trunk_commit_id = trunk_commit_id
    for revision in plan.planned_revisions:
        console.output(
            t"Merging PR #{revision.identity.pr_number} for "
            t"{revision.subject} {ui.change_id(revision.change_id)}..."
        )
        final_pull_request, blocked = await merge_pull_request(
            context=execution.context,
            github_client=github_client,
            revision=revision,
            merge_method=merge_method,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            trunk_commit_id=current_trunk_commit_id,
        )
        if blocked is not None or final_pull_request is None:
            blocked_action = blocked
            break
        actions.append(
            MergeAction(
                kind="pull request",
                body=t"merge PR #{revision.identity.pr_number} into "
                t"{ui.bookmark(trunk_branch)} on GitHub for "
                t"{revision.subject} {ui.change_id(revision.change_id)}",
                status="applied",
            )
        )
        merged_change_ids.append(revision.change_id)
        try:
            execution.context.jj_client.fetch_remote(
                remote=remote_name,
                branches=(trunk_branch,),
            )
            current_trunk_commit_id = execution.context.jj_client.resolve_revision(
                "trunk()"
            ).commit_id
        except (CliError, JjCommandError) as error:
            blocked_action = MergeAction(
                kind="boundary",
                body=t"after accepted {ui.change_id(revision.change_id)}: could not "
                t"refresh trunk: {error}; run {ui.cmd(f'sync {selected_revset}')}",
                status="blocked",
            )
            break

    if blocked_action is not None:
        actions.append(blocked_action)
    blocked = blocked_action is not None or len(merged_change_ids) != len(plan.planned_revisions)
    if not blocked and plan.boundary_action is not None:
        actions.append(plan.boundary_action)
    return merge_result(
        actions=tuple(actions),
        applied=bool(merged_change_ids),
        blocked=blocked,
        merged_change_ids=tuple(merged_change_ids),
    )
