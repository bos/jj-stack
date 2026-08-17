"""Apply complete sync convergence plans."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands.cleanup.command import cleanup_tracked_prs
from jj_stack.commands.submit.command import run_submit_async
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.render import print_submit_result
from jj_stack.errors import CliError, ConflictedStackError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubTarget
from jj_stack.jj.client import PRRefUpdate
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import SubmittedBaseline, TrackedPR
from jj_stack.stack.convergence_models import (
    ConvergenceActions,
    GithubStackMergePlan,
    GithubStackRebasePlan,
    PRFinishPlan,
    SelectedConvergencePlan,
    SkipPRFinish,
)
from jj_stack.stack.convergence_observation import dependent_path_heads
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class PRFinishResult:
    candidate: TrackedPR
    outcome: Literal["finished", "already_terminal", "skipped"]
    skip_reason: Message | None = None


async def apply_pr_finishes(
    *,
    plans: tuple[PRFinishPlan, ...],
    dry_run: bool,
    github: GithubClient,
    trunk_branch: str,
) -> tuple[PRFinishResult, ...]:
    results: list[PRFinishResult] = []
    for plan in plans:
        results.append(
            await _apply_pr_finish(
                plan=plan,
                dry_run=dry_run,
                github=github,
                trunk_branch=trunk_branch,
            )
        )
    visible = tuple(result for result in results if result.outcome != "already_terminal")
    if visible:
        console.output(
            "Planned GitHub updates for merged PRs:"
            if dry_run
            else "Applied GitHub updates for merged PRs:"
        )
        marker = "•" if dry_run else "✓"
        for result in visible:
            if result.outcome == "skipped":
                console.output(
                    t"  ! leave {ui.change_id(result.candidate.change_id)} unchanged: "
                    t"{result.skip_reason}"
                )
            else:
                console.output(
                    t"  {marker} finish merged PR #{result.candidate.pr_identity.pr_number}"
                )
    return tuple(results)


async def _apply_pr_finish(
    *, plan: PRFinishPlan, dry_run: bool, github: GithubClient, trunk_branch: str
) -> PRFinishResult:
    candidate = plan.candidate
    if isinstance(plan, SkipPRFinish):
        return PRFinishResult(candidate, "already_terminal")
    if dry_run:
        return PRFinishResult(candidate, "finished")
    console.output(t"Finishing PR #{plan.pr.number} for {candidate.change_id}...")
    current = plan.pr
    try:
        if current.base.ref != trunk_branch:
            current = (
                await github.update_pr(pr_number=current.number, base=trunk_branch)
            ).normalize_state()
        if current.state == "open" and current.base.ref != trunk_branch:
            reason: Message | None = t"PR #{current.number} did not stay retargeted to trunk"
        else:
            if current.state == "open":
                await github.close_pr(pr_number=current.number)
            reason = None
    except GithubClientError as error:
        reason = t"could not finish cleanup for PR #{current.number}: {error}"
    return (
        PRFinishResult(candidate, "skipped", reason)
        if reason
        else PRFinishResult(candidate, "finished")
    )


async def apply_selected_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
    plan: SelectedConvergencePlan,
    target: GithubTarget,
    trunk_branch: str,
    trunk_commit_id: str,
) -> int:
    """Apply one complete selected convergence plan in dependency order."""

    actions = plan.actions
    if isinstance(plan, GithubStackRebasePlan):
        if not dry_run:
            _apply_github_stack_rebase(
                context=context,
                plan=plan,
                remote_name=target.remote.name,
                trunk_commit_id=trunk_commit_id,
            )
        return 0
    results = await apply_pr_finishes(
        plans=tuple(change.finish for change in actions.on_trunk),
        dry_run=dry_run,
        github=github,
        trunk_branch=trunk_branch,
    )
    dependencies = _apply_local_convergence(
        context=context,
        dry_run=dry_run,
        plan=plan,
        remote_name=target.remote.name,
        trunk_commit_id=trunk_commit_id,
    )
    update_result = await _refresh_selected_prs(
        actions=actions,
        context=context,
        dry_run=dry_run,
    )
    if update_result != 0:
        return update_result
    return await _cleanup_reconciled_prs(
        context=context,
        dry_run=dry_run,
        finish_results=results,
        github=github,
        submitted_survivors=actions.submitted_survivors,
        dependencies=dependencies,
        target=target,
    )


def _apply_local_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: SelectedConvergencePlan,
    remote_name: str,
    trunk_commit_id: str,
) -> dict[str, tuple[LocalCommit, ...]]:
    actions = plan.actions
    adopted = plan.adopted_survivors if isinstance(plan, GithubStackMergePlan) else ()
    adopted_ids = {item.candidate.change_id for item in adopted}
    commit_ids = (
        (
            *(item.commit_id for item in actions.survivors if item.change_id not in adopted_ids),
            *actions.working_copy_children,
        )
        if actions.on_trunk
        else ()
    )
    if dry_run:
        return _observe_removal_dependencies(context=context, actions=actions)
    if adopted:
        top = adopted[-1]
        replaced = tuple(
            item.local_change.commit_id
            for item in adopted
            if item.local_change.commit_id != item.remote_commit_id
        )
        destination = top.remote_commit_id
        attachment = context.jj_client.import_remote_pr_branch_ref(
            remote=remote_name,
            branch=top.candidate.pr_identity.head_ref,
            expected_target=destination,
            expected_change_id=top.candidate.change_id,
            expected_chain=tuple(
                (
                    item.candidate.pr_identity.head_ref,
                    item.remote_commit_id,
                    item.candidate.change_id,
                )
                for item in adopted
            ),
            expected_parent_commit_id=trunk_commit_id,
        )
    else:
        replaced = ()
        destination = trunk_commit_id
        attachment = nullcontext()
    with attachment:
        if commit_ids:
            context.jj_client.rebase_exact_commits(
                commit_ids=commit_ids,
                destination=destination,
            )
        if replaced:
            context.jj_client.abandon_changes(replaced)
        dependencies = _observe_removal_dependencies(context=context, actions=actions)
        abandoned = tuple(
            change.change.commit_id
            for change in actions.on_trunk
            if change.change is not None
            and not change.change.immutable
            and not dependencies.get(change.candidate.change_id)
        )
        if abandoned:
            context.jj_client.abandon_changes(abandoned)
        if adopted:
            context.state_store.relink_prs(
                replacements={
                    item.candidate.change_id: (
                        item.candidate.pr_identity,
                        SubmittedBaseline(commit_id=item.remote_commit_id),
                    )
                    for item in adopted
                },
            )
    return dependencies


def _apply_github_stack_rebase(
    *,
    context: CommandContext,
    plan: GithubStackRebasePlan,
    remote_name: str,
    trunk_commit_id: str,
) -> None:
    adopted = plan.adopted_survivors
    top = adopted[-1]
    with context.jj_client.import_remote_pr_branch_ref(
        remote=remote_name,
        branch=top.candidate.pr_identity.head_ref,
        expected_target=top.remote_commit_id,
        expected_chain=tuple(
            (
                item.candidate.pr_identity.head_ref,
                item.remote_commit_id,
                (None, item.candidate.change_id),
            )
            for item in adopted
        ),
        expected_parent_commit_id=trunk_commit_id,
    ):
        desired, operation_id = _verified_local_rebase(
            context=context,
            plan=plan,
            trunk_commit_id=trunk_commit_id,
        )
        if operation_id is not None:
            context.jj_client.integrate_operation(operation_id)
        desired_by_change = {item.change_id: item for item in desired}
        context.jj_client.mutate_remote_pr_branch_refs(
            remote=remote_name,
            updates=tuple(
                PRRefUpdate(
                    branch=item.candidate.pr_identity.head_ref,
                    expected_target=item.remote_commit_id,
                    desired_target=desired_by_change[item.candidate.change_id].commit_id,
                )
                for item in adopted
            ),
        )
        context.state_store.relink_prs(
            replacements={
                item.candidate.change_id: (
                    item.candidate.pr_identity,
                    SubmittedBaseline(
                        commit_id=desired_by_change[item.candidate.change_id].commit_id
                    ),
                )
                for item in adopted
            },
        )


def _verified_local_rebase(
    *,
    context: CommandContext,
    plan: GithubStackRebasePlan,
    trunk_commit_id: str,
) -> tuple[tuple[LocalCommit, ...], str | None]:
    adopted = plan.adopted_survivors
    local = plan.actions.survivors
    desired = local
    operation_id: str | None = None
    if all(
        item.local_change.commit_id == item.candidate.submitted_baseline.commit_id
        for item in adopted
    ):
        operation_id = context.jj_client.prepare_rebase_exact_commits(
            commit_ids=(
                *tuple(item.commit_id for item in local),
                *plan.actions.working_copy_children,
            ),
            destination=trunk_commit_id,
        )
        grouped = context.jj_client.query_commits_at_operation(
            change_ids=tuple(item.change_id for item in local),
            operation_id=operation_id,
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
                t"Rebasing {ui.change_id(change.change_id)} locally produced conflicts.",
                hint=t"Rebase and resolve the stack with {ui.cmd('jj')}, then run "
                t"{ui.cmd('jj-stack submit')}.",
            )
        if change.parents != (expected_parent,):
            raise CliError(
                "The local stack does not match GitHub's rebase onto fetched trunk.",
                hint=t"Inspect the local and GitHub stacks, then restore or resubmit the "
                t"intended pull requests.",
            )
        expected_parent = change.commit_id
    desired_by_change = {item.change_id: item for item in desired}
    tree_pairs = tuple(
        (desired_by_change[item.candidate.change_id].commit_id, item.remote_commit_id)
        for item in adopted
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
    return desired, operation_id


async def _refresh_selected_prs(
    *, actions: ConvergenceActions, context: CommandContext, dry_run: bool
) -> int:
    if not actions.on_trunk:
        return 0
    if actions.survivors and dry_run:
        console.output(
            t"Run {ui.cmd(f'jj-stack sync {actions.survivors[-1].change_id}')} to apply the "
            t"rebase and then compute updates for the remaining existing PRs."
        )
        return 0
    if not actions.submitted_survivors:
        if actions.survivors:
            console.output("No existing pull requests to update; trailing work remains local.")
        return 0
    head_change_id = actions.submitted_survivors[-1].change_id
    try:
        result = await run_submit_async(
            context=context,
            on_prepared=None,
            options=_sync_submit_options(dry_run=dry_run, revset=head_change_id),
        )
    except ConflictedStackError as error:
        raise ConflictedStackError(
            error.message,
            hint=t"The local rebase is complete. Resolve the conflicts with {ui.cmd('jj')}, "
            t"then update the remaining pull requests with "
            t"{ui.cmd(f'jj-stack submit {head_change_id}')}",
        ) from error
    print_submit_result(result)
    return 0


async def _cleanup_reconciled_prs(
    *,
    context: CommandContext,
    dry_run: bool,
    finish_results: tuple[PRFinishResult, ...],
    github: GithubClient,
    submitted_survivors: tuple[LocalCommit, ...],
    dependencies: dict[str, tuple[LocalCommit, ...]],
    target: GithubTarget,
) -> int:
    cleanup_candidates: list[TrackedPR] = []
    for result in finish_results:
        if result.outcome == "skipped":
            continue
        if heads := dependencies.get(result.candidate.change_id):
            recovery = (
                t"run {ui.join(lambda r: ui.cmd(f'jj-stack sync {r.change_id[:8]}'), heads)}"
            )
            console.output(
                t"  ! kept PR #{result.candidate.pr_identity.pr_number} and its PR "
                t"branch for {ui.change_id(result.candidate.change_id)}: another local stack "
                t"still uses this merged change; {recovery}"
            )
            continue
        cleanup_candidates.append(result.candidate)
    pr_identities = context.state_store.load().pr_identities
    cleanup = await cleanup_tracked_prs(
        change_ids=tuple(item.change_id for item in cleanup_candidates),
        context=context,
        dry_run=dry_run,
        github_client=github,
        github_target=target,
        planned_detached_dependents=frozenset(
            identity.pr_number
            for change in submitted_survivors
            if (identity := pr_identities.get(change.change_id)) is not None
        ),
        planned_local_removals=frozenset(item.change_id for item in cleanup_candidates),
    )
    return 1 if any(action.status == "blocked" for action in cleanup.actions) else 0


def _observe_removal_dependencies(
    *, context: CommandContext, actions: ConvergenceActions
) -> dict[str, tuple[LocalCommit, ...]]:
    anchors = {
        change.candidate.change_id: (
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
                *(change.candidate.change_id for change in actions.on_trunk),
                *(item.change_id for item in actions.survivors),
            )
        ),
    )
    return {change_id: observed.get(commit_id, ()) for change_id, commit_id in anchors.items()}


def _sync_submit_options(*, dry_run: bool, revset: str) -> SubmitOptions:
    return SubmitOptions(
        base_revset=None,
        descriptions=(),
        describe_with=None,
        draft_mode="default",
        dry_run=dry_run,
        edit=False,
        existing_only=True,
        labels=None,
        re_request=False,
        reviewers=None,
        revset=revset,
        team_reviewers=None,
    )
