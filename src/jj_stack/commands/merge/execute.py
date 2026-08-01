"""Apply an ordinary pull-request merge for a one-PR review.

The mutation rechecks fresh local, remote, and GitHub observations. GitHub moves trunk; this
command does not rewrite local history or retire review tracking. A later `sync` observes and
reconciles whatever GitHub accepted.
"""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.github.client import GithubClient

from .models import (
    MergeAction,
    MergeExecutionInputs,
    MergePlan,
    MergeResult,
)
from .pull_requests import merge_pull_request


async def execute_single_pull_request_merge(
    *,
    github_client: GithubClient,
    merge_method: str,
    plan: MergePlan,
    execution: MergeExecutionInputs,
) -> MergeResult:
    """Merge the sole planned revision through GitHub's ordinary PR API."""

    if plan.blocked:
        return execution.result(actions=plan.planned_actions())
    if len(plan.planned_revisions) != 1:
        raise AssertionError("An ordinary merge requires exactly one planned revision.")

    execution.context.state_store.require_writable()
    revision = plan.planned_revisions[0]
    console.output(
        t"Merging PR #{revision.identity.pr_number} for "
        t"{revision.subject} {ui.change_id(revision.change_id)}..."
    )
    final_pull_request, blocked_action = await merge_pull_request(
        context=execution.context,
        github_client=github_client,
        revision=revision,
        merge_method=merge_method,
        remote_name=execution.remote_name,
        stack_selector=execution.selected_revset,
        trunk_branch=execution.trunk_branch,
    )
    if blocked_action is not None:
        return execution.result(actions=(blocked_action,))
    if final_pull_request is None:
        raise AssertionError("An ordinary merge returned neither a pull request nor a blocker.")

    applied_action = MergeAction(
        kind="pull request",
        body=t"merge PR #{revision.identity.pr_number} into "
        t"{ui.bookmark(execution.trunk_branch)} on GitHub for "
        t"{revision.subject} {ui.change_id(revision.change_id)}",
        status="applied",
    )
    actions = (
        (applied_action, plan.boundary_action)
        if plan.boundary_action is not None
        else (applied_action,)
    )
    return execution.result(
        actions=actions,
        merged_change_ids=(revision.change_id,),
    )
