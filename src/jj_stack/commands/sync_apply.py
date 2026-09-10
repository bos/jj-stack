"""Apply planned local rebases, PR updates, and cleanup."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands.cleanup.command import cleanup_tracked_prs
from jj_stack.commands.sync_prs import refresh_selected_prs
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubTarget
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import PRRefUpdate
from jj_stack.models.github import GithubPR, GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import SubmittedBaseline, TrackedPR
from jj_stack.stack.convergence import divergent_change_error
from jj_stack.stack.convergence_models import (
    ConvergenceActions,
    GithubStackMergePlan,
    GithubStackRebasePlan,
    OnTrunkChange,
    RewrittenPRChange,
    SelectedConvergencePlan,
)
from jj_stack.stack.convergence_observation import dependent_path_heads
from jj_stack.stack.observation import observe_change_copies, observe_pr_bookmarks
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class PRFinishResult:
    change_id: str
    candidate: TrackedPR
    outcome: Literal["finished", "already_terminal", "skipped"]
    skip_reason: Message | None = None


async def apply_pr_finishes(
    *,
    plans: tuple[OnTrunkChange, ...],
    dry_run: bool,
    github: GithubClient,
) -> tuple[PRFinishResult, ...]:
    results: list[PRFinishResult] = []
    for plan in plans:
        results.append(
            await _apply_pr_finish(
                plan=plan,
                dry_run=dry_run,
                github=github,
            )
        )
    visible = tuple(result for result in results if result.outcome != "already_terminal")
    if visible:
        console.output(
            "Planned PR updates for changes already on trunk:"
            if dry_run
            else "PR updates for changes already on trunk:"
        )
        marker = "•" if dry_run else "✓"
        for result in visible:
            if result.outcome == "skipped":
                console.output(
                    t"  ! leave {ui.change_id(result.change_id)} unchanged: {result.skip_reason}"
                )
            else:
                pr_label = format_pr_label(
                    result.candidate.pr_identity.pr_number,
                    repo=github.repo,
                )
                console.output(
                    t"  {marker} close {pr_label} for {ui.change_id(result.change_id)}"
                )
    return tuple(results)


async def _apply_pr_finish(
    *, plan: OnTrunkChange, dry_run: bool, github: GithubClient
) -> PRFinishResult:
    candidate = plan.candidate
    pr = plan.close_pr
    if pr is None:
        return PRFinishResult(plan.change_id, candidate, "already_terminal")
    if dry_run:
        return PRFinishResult(plan.change_id, candidate, "finished")
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    try:
        await github.close_pr(pr_number=pr.number)
    except GithubClientError as error:
        return PRFinishResult(
            plan.change_id,
            candidate,
            "skipped",
            t"cannot close {pr_label}: {error.user_facing_reason()}",
        )
    return PRFinishResult(plan.change_id, candidate, "finished")


async def apply_selected_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
    plan: SelectedConvergencePlan,
    github_stacks: tuple[GithubStack, ...],
    trunk_branch: str,
    target: GithubTarget,
    trunk_commit_id: CommitId,
) -> int:
    """Apply a stack sync plan in dependency order."""

    actions = plan.actions
    if isinstance(plan, GithubStackRebasePlan):
        _apply_github_stack_rebase(
            context=context,
            dry_run=dry_run,
            plan=plan,
            remote_name=target.remote.name,
            trunk_commit_id=trunk_commit_id,
        )
        return 0
    results = await apply_pr_finishes(
        plans=actions.on_trunk,
        dry_run=dry_run,
        github=github,
    )
    dependencies = _apply_local_convergence(
        context=context,
        dry_run=dry_run,
        plan=plan,
        remote_name=target.remote.name,
        trunk_commit_id=trunk_commit_id,
    )
    await refresh_selected_prs(
        actions=actions,
        context=context,
        dry_run=dry_run,
        github=github,
        github_stacks=github_stacks,
        target=target,
        trunk_branch=trunk_branch,
    )
    return await _cleanup_reconciled_prs(
        context=context,
        dry_run=dry_run,
        finish_results=results,
        github=github,
        remaining_prs=actions.remaining_prs,
        dependencies=dependencies,
        target=target,
    )


def _apply_local_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: SelectedConvergencePlan,
    remote_name: str,
    trunk_commit_id: CommitId,
) -> dict[str, tuple[LocalCommit, ...]]:
    actions = plan.actions
    rewritten = plan.rewritten_changes if isinstance(plan, GithubStackMergePlan) else ()
    # GitHub rewrites each remaining PR from its submitted baseline. Use GitHub's commits only
    # when every local change is still at that baseline; otherwise rebase and resubmit them all.
    adopt = _all_at_baseline(rewritten)
    adopted_ids = {item.change_id for item in rewritten} if adopt else set()
    rebased = (
        (
            *(item for item in actions.remaining_changes if item.change_id not in adopted_ids),
            *actions.working_copy_children,
        )
        if actions.on_trunk
        else ()
    )
    if dry_run:
        return _observe_removal_dependencies(context=context, actions=actions)
    if isinstance(plan, GithubStackMergePlan) and adopt and rewritten:
        top = rewritten[-1]
        replaced = tuple(
            item.local_change.commit_id
            for item in rewritten
            if item.local_change.commit_id != item.pr.head.sha
        )
        destination = top.pr.head.sha
        attachment = context.jj_client.import_remote_pr_branch_ref(
            remote=remote_name,
            branch=top.candidate.pr_identity.head_ref,
            expected_target=destination,
            expected_change_id=top.change_id,
            expected_chain=tuple(
                (
                    item.candidate.pr_identity.head_ref,
                    item.pr.head.sha,
                    item.change_id,
                )
                for item in rewritten
            ),
            expected_parent_commit_id=plan.expected_parent_commit_id,
        )
    else:
        replaced = ()
        destination = trunk_commit_id
        attachment = nullcontext()
    with attachment:
        if rebased:
            change_ids, rewrite_args = _single_visible_change_ids(context, rebased)
            context.jj_client.rebase_changes(
                change_ids=change_ids, destination=destination, cli_args=rewrite_args
            )
        else:
            rewrite_args, _snapshots = observe_pr_bookmarks(
                jj_client=context.jj_client, state=context.state_store.load()
            )
        if replaced:
            context.jj_client.abandon_commits(replaced, cli_args=rewrite_args)
        dependencies = _observe_removal_dependencies(context=context, actions=actions)
        abandoned = tuple(
            change.change.commit_id
            for change in actions.on_trunk
            if change.change is not None
            and not change.change.immutable
            and not dependencies.get(change.change_id)
        )
        if abandoned:
            context.jj_client.abandon_commits(abandoned, cli_args=rewrite_args)
        if rewritten:
            context.state_store.relink_prs(
                replacements={
                    item.change_id: TrackedPR(
                        pr_identity=item.candidate.pr_identity,
                        submitted_baseline=SubmittedBaseline(commit_id=item.pr.head.sha),
                    )
                    for item in rewritten
                },
            )
    return dependencies


def _apply_github_stack_rebase(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: GithubStackRebasePlan,
    remote_name: str,
    trunk_commit_id: CommitId,
) -> None:
    adopted = plan.rewritten_changes
    top = adopted[-1]
    with context.jj_client.import_remote_pr_branch_ref(
        remote=remote_name,
        branch=top.candidate.pr_identity.head_ref,
        expected_target=top.pr.head.sha,
        expected_chain=tuple(
            (
                item.candidate.pr_identity.head_ref,
                item.pr.head.sha,
                (None, item.change_id),
            )
            for item in adopted
        ),
        expected_parent_commit_id=trunk_commit_id,
    ):
        desired_by_change, operation_id = _verified_local_rebase(
            context=context,
            plan=plan,
            trunk_commit_id=trunk_commit_id,
        )
        if dry_run:
            return
        if operation_id is not None:
            context.jj_client.integrate_operation(operation_id)
        context.jj_client.mutate_remote_pr_branch_refs(
            remote=remote_name,
            updates=tuple(
                PRRefUpdate(
                    branch=item.candidate.pr_identity.head_ref,
                    expected_target=item.pr.head.sha,
                    desired_target=desired_by_change[item.change_id].commit_id,
                )
                for item in adopted
            ),
        )
        context.state_store.relink_prs(
            replacements={
                item.change_id: TrackedPR(
                    pr_identity=item.candidate.pr_identity,
                    submitted_baseline=SubmittedBaseline(
                        commit_id=desired_by_change[item.change_id].commit_id
                    ),
                )
                for item in adopted
            },
        )


def _verified_local_rebase(
    *,
    context: CommandContext,
    plan: GithubStackRebasePlan,
    trunk_commit_id: CommitId,
) -> tuple[dict[str, LocalCommit], str | None]:
    adopted = plan.rewritten_changes
    local = plan.actions.remaining_changes
    desired = local
    operation_id: str | None = None
    if _all_at_baseline(adopted):
        change_ids, rewrite_args = _single_visible_change_ids(
            context, (*local, *plan.actions.working_copy_children)
        )
        operation_id = context.jj_client.prepare_rebase_changes(
            change_ids=change_ids, destination=trunk_commit_id, cli_args=rewrite_args
        )
        grouped = context.jj_client.query_commits_at_operation(
            change_ids=tuple(item.change_id for item in local),
            operation_id=operation_id,
            cli_args=rewrite_args,
        )
        desired = tuple(
            commits[0] for item in local if len(commits := grouped[item.change_id]) == 1
        )
        if len(desired) != len(local):
            raise CliError(
                "A local change did not have exactly one commit after rebasing onto trunk."
            )
    expected_parent = trunk_commit_id
    for change in desired:
        if change.conflict:
            raise CliError(
                t"A local rebase of {ui.change_id(change.change_id)} would produce conflicts.",
                hint=t"Rebase and resolve the local stack to match GitHub's version, then "
                t"rerun the same {ui.cmd('jj-stack sync')} command.",
            )
        if change.parents != (expected_parent,):
            raise CliError(
                "The local stack does not match GitHub's rebase onto trunk.",
                hint=t"Compare the local history with the PR branches on GitHub, then "
                t"restore the intended change order with {ui.cmd('jj')}. Run "
                t"{ui.cmd('jj-stack sync')} again when the stacks match.",
            )
        expected_parent = change.commit_id
    desired_by_change: dict[str, LocalCommit] = {item.change_id: item for item in desired}
    tree_pairs = tuple(
        (desired_by_change[item.change_id].commit_id, item.pr.head.sha) for item in adopted
    )
    trees = context.jj_client.git_tree_ids(
        tuple(commit_id for pair in tree_pairs for commit_id in pair)
    )
    if any(trees[local_id] != trees[remote_id] for local_id, remote_id in tree_pairs):
        raise CliError(
            "GitHub's rewritten stack does not have the same contents as the local rebase.",
            hint=t"Inspect the changed PR branches on GitHub before choosing which version "
            t"to keep.",
        )
    return desired_by_change, operation_id


def _all_at_baseline(items: tuple[RewrittenPRChange, ...]) -> bool:
    return all(
        item.local_change.commit_id == item.candidate.submitted_baseline.commit_id
        for item in items
    )


def _single_visible_change_ids(
    context: CommandContext, changes: tuple[LocalCommit, ...]
) -> tuple[tuple[ChangeId, ...], JjCliArgs]:
    """Require one visible commit per change right before rewriting it.

    Planning observed these changes before the GitHub round-trips; one that became divergent
    since then must not be rewritten at all.
    """

    change_ids = tuple(change.change_id for change in changes)
    observed = observe_change_copies(
        jj_client=context.jj_client, state=context.state_store.load(), change_ids=change_ids
    )
    visible = observed.copies(change_ids)
    for change_id in change_ids:
        if len(visible[change_id]) != 1:
            raise divergent_change_error(change_id)
    return change_ids, observed.cli_args


async def _cleanup_reconciled_prs(
    *,
    context: CommandContext,
    dry_run: bool,
    finish_results: tuple[PRFinishResult, ...],
    github: GithubClient,
    remaining_prs: dict[str, GithubPR],
    dependencies: dict[str, tuple[LocalCommit, ...]],
    target: GithubTarget,
) -> int:
    cleanup_change_ids: list[str] = []
    for result in finish_results:
        if result.outcome == "skipped":
            continue
        if heads := dependencies.get(result.change_id):
            recovery_commands = tuple(
                f"jj-stack sync {short_change_id(head.change_id)}" for head in heads
            )
            recovery = t"run {ui.join(ui.cmd, recovery_commands)}"
            pr_label = format_pr_label(
                result.candidate.pr_identity.pr_number,
                repo=target.repo,
            )
            console.output(
                t"  ! kept the saved link and PR branch for {pr_label} "
                t"({ui.change_id(result.change_id)}): another local stack "
                t"still uses this merged change; {recovery}"
            )
            continue
        cleanup_change_ids.append(result.change_id)
    cleanup = await cleanup_tracked_prs(
        change_ids=tuple(cleanup_change_ids),
        context=context,
        dry_run=dry_run,
        github_client=github,
        github_target=target,
        planned_detached_dependents=frozenset(pr.number for pr in remaining_prs.values()),
        planned_local_removals=frozenset(cleanup_change_ids),
    )
    return 1 if any(action.status == "blocked" for action in cleanup.actions) else 0


def _observe_removal_dependencies(
    *, context: CommandContext, actions: ConvergenceActions
) -> dict[str, tuple[LocalCommit, ...]]:
    anchors = {
        change.change_id: (
            change.change.commit_id
            if change.change is not None
            else change.candidate.submitted_baseline.commit_id
        )
        for change in actions.on_trunk
        if change.evidence_kind == "rewritten"
    }
    observed = dependent_path_heads(
        ancestor_commit_ids=tuple(anchors.values()),
        context=context,
        excluded_change_ids=frozenset(
            (
                *(change.change_id for change in actions.on_trunk),
                *(item.change_id for item in actions.remaining_changes),
            )
        ),
    )
    return {change_id: observed.get(commit_id, ()) for change_id, commit_id in anchors.items()}
