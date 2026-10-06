"""Apply planned local rebases, PR updates, and cleanup."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands.cleanup.command import cleanup_tracked_prs
from jj_stack.commands.github_run import GithubRun, ObservedTrunk
from jj_stack.commands.sync_prs import refresh_selected_prs
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClientError
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.models.github import GithubPR, GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.convergence_models import OnTrunkChange, SelectedConvergencePlan
from jj_stack.stack.convergence_observation import dependent_path_heads
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class PRFinishResult:
    change_id: ChangeId
    candidate: TrackedPR
    outcome: Literal["finished", "already_terminal", "skipped"]
    skip_reason: Message | None = None


async def apply_pr_finishes(
    run: GithubRun, plans: tuple[OnTrunkChange, ...]
) -> tuple[PRFinishResult, ...]:
    dry_run = run.dry_run
    results = [await _apply_pr_finish(run, plan) for plan in plans]
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
                    repo=run.github.repo,
                )
                console.output(
                    t"  {marker} close {pr_label} for {ui.change_id(result.change_id)}"
                )
    return tuple(results)


async def _apply_pr_finish(run: GithubRun, plan: OnTrunkChange) -> PRFinishResult:
    candidate = plan.candidate
    pr = plan.close_pr
    if pr is None:
        return PRFinishResult(plan.change_id, candidate, "already_terminal")
    if run.dry_run:
        return PRFinishResult(plan.change_id, candidate, "finished")
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    try:
        await run.github.close_pr(pr_number=pr.number)
    except GithubClientError as error:
        return PRFinishResult(
            plan.change_id,
            candidate,
            "skipped",
            t"cannot close {pr_label}: {error.user_facing_reason()}",
        )
    return PRFinishResult(plan.change_id, candidate, "finished")


async def apply_selected_convergence(
    run: GithubRun,
    *,
    plan: SelectedConvergencePlan,
    github_stacks: tuple[GithubStack, ...],
    trunk: ObservedTrunk,
    trunk_commit_id: CommitId,
) -> int:
    """Apply a stack sync plan in dependency order."""

    run = replace(run, trunk=trunk)
    results = await apply_pr_finishes(run, plan.on_trunk)
    dependencies = _apply_local_convergence(run, plan=plan, trunk_commit_id=trunk_commit_id)
    await refresh_selected_prs(
        run,
        plan=plan,
        github_stacks=github_stacks,
        trunk=trunk,
    )
    return await _cleanup_reconciled_prs(
        run,
        finish_results=results,
        remaining_prs=plan.remaining_prs,
        dependencies=dependencies,
    )


def _apply_local_convergence(
    run: GithubRun, *, plan: SelectedConvergencePlan, trunk_commit_id: CommitId
) -> dict[ChangeId, tuple[LocalCommit, ...]]:
    context = run.context
    if run.dry_run:
        return _observe_removal_dependencies(context=context, plan=plan)
    # Hiding GitHub's copies first leaves one copy of each change to rebase and publish.
    context.jj_client.abandon_commits(plan.github_copies)
    if plan.destination is not None:
        context.jj_client.rebase_changes(
            change_ids=tuple(
                change.change_id
                for change in (*plan.remaining_changes, *plan.working_copy_children)
            ),
            destination=plan.destination,
        )
    dependencies = _observe_removal_dependencies(context=context, plan=plan)
    abandoned = tuple(
        change.change.commit_id
        for change in plan.on_trunk
        if change.change is not None
        and not change.change.immutable
        and not dependencies.get(change.change_id)
    )
    if abandoned:
        if any(
            change.change.current_working_copy
            for change in plan.on_trunk
            if change.change is not None and change.change.commit_id in abandoned
        ):
            context.jj_client.new_empty_change(trunk_commit_id)
        context.jj_client.abandon_commits(abandoned)
    return dependencies


async def _cleanup_reconciled_prs(
    run: GithubRun,
    *,
    finish_results: tuple[PRFinishResult, ...],
    remaining_prs: dict[ChangeId, GithubPR],
    dependencies: dict[ChangeId, tuple[LocalCommit, ...]],
) -> int:
    cleanup_change_ids: list[ChangeId] = []
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
                repo=run.target.repo,
            )
            console.output(
                t"  ! kept the saved link and PR branch for {pr_label} "
                t"({ui.change_id(result.change_id)}): another local stack "
                t"still uses this merged change; {recovery}"
            )
            continue
        cleanup_change_ids.append(result.change_id)
    blocked = await cleanup_tracked_prs(
        run,
        change_ids=tuple(cleanup_change_ids),
        planned_detached_dependents=frozenset(pr.number for pr in remaining_prs.values()),
        planned_local_removals=frozenset(cleanup_change_ids),
    )
    return 1 if blocked else 0


def _observe_removal_dependencies(
    *, context: CommandContext, plan: SelectedConvergencePlan
) -> dict[ChangeId, tuple[LocalCommit, ...]]:
    anchors = {
        change.change_id: (
            change.change.commit_id
            if change.change is not None
            else change.candidate.submitted_baseline.commit_id
        )
        for change in plan.on_trunk
        if change.evidence_kind == "rewritten"
    }
    observed = dependent_path_heads(
        ancestor_commit_ids=tuple(anchors.values()),
        context=context,
        excluded_change_ids=frozenset(
            (
                *(change.change_id for change in plan.on_trunk),
                *(item.change_id for item in plan.remaining_changes),
            )
        ),
    )
    return {change_id: observed.get(commit_id, ()) for change_id, commit_id in anchors.items()}
