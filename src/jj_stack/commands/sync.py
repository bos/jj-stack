"""Update a local stack after GitHub merges changes from its bottom.

`sync` fetches trunk, verifies which submitted changes GitHub merged, removes their old local
copies when safe, rebases the remaining selected changes onto trunk, and refreshes only pull
requests that already exist for them. It does not submit trailing unreviewed work or touch sibling
stacks.

Preview with `jj-stack sync --dry-run <head-change-id>`. If trunk advanced but none of this
stack's changes merged, rebase only the intended path with `jj` instead.

`sync --all` is repository-wide cleanup for reviews whose exact last-submitted commits are
already on trunk. It does not rebase stacks or handle merge results that GitHub rewrote.

Common examples: `jj-stack sync --dry-run <head-change-id>` previews a selected update;
`jj-stack sync <head-change-id>` applies it; and `jj-stack sync --all --dry-run` previews
repository-wide cleanup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.submit.command import print_selected_line, run_submit_async
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.render import print_submit_result
from jj_stack.commands.sync_global import run_global_recovery
from jj_stack.errors import CliError, UsageError
from jj_stack.github.client import GithubClient, build_github_client
from jj_stack.github.resolution import GithubTarget, resolve_trunk_branch
from jj_stack.jj.client import JjCliArgs, UnsupportedStackError
from jj_stack.models.review_state import SubmittedBaseline
from jj_stack.models.stack import LocalRevision
from jj_stack.review.convergence import (
    SelectedConvergencePlan,
    build_selected_convergence_plan,
    rewritten_retirement_blocker,
    selected_rebase_revision_ids,
)
from jj_stack.review.landed import (
    FinalizationContext,
    LandedReviewResult,
    finalize_landed_reviews,
    render_landed_results,
    retire_landed_reviews,
)
from jj_stack.review.landed_evidence import LandedReviewCandidate
from jj_stack.review.native_sync import resolve_selected_native_observation
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_review_mutation,
)
from jj_stack.review.status import PreparedStatus, prepare_status, status_preparation_cli_error
from jj_stack.state.operation_lock import acquire_operation_lock
from jj_stack.ui import Message

HELP = "Update a stack after GitHub merges or clean up merged PRs"


def sync(
    *,
    all_: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    if all_ and revset is not None:
        raise UsageError(t"Use either {ui.cmd('jj-stack sync --all')} or a revision, not both.")
    context = bootstrap_context(repository=repository, cli_args=cli_args, debug=debug)
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="sync --all" if all_ else "sync",
    ):
        if all_:
            return asyncio.run(run_global_recovery(context=context, dry_run=dry_run))
        return run_stack_convergence(
            context=context,
            dry_run=dry_run,
            print_selected=revset is None,
            revset=revset or "@-",
        )


def run_stack_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    fetch_remote_state: bool = True,
    print_selected: bool = False,
    revset: str,
) -> int:
    try:
        prepared_status = prepare_status(
            context=context,
            fetch_remote_state=fetch_remote_state,
            re_resolve_after_remote_refresh=True,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    if print_selected and prepared_status.prepared.stack.revisions:
        head = prepared_status.prepared.stack.head
        print_selected_line(head.change_id, head.subject)
    return asyncio.run(
        _run_selected_convergence(
            context=context,
            dry_run=dry_run,
            prepared_status=prepared_status,
        )
    )


async def _run_selected_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    prepared_status: PreparedStatus,
) -> int:
    prepared = prepared_status.prepared
    target, selected = _selected_target(prepared_status)
    if not selected:
        console.output("Nothing to sync: the selected revision is already on trunk.")
        return 0
    async with build_github_client(repository=target.repository) as github:
        repository_state = await github.get_repository()
        trunk_branch = resolve_trunk_branch(
            bookmark_states=prepared.bookmark_states,
            github_repository_state=repository_state,
            remote_name=target.remote.name,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        observation = await observe_review_mutation(
            change_ids=tuple(revision.change_id for revision in selected),
            context=context,
            github_client=github,
            remote_name=target.remote.name,
            trunk_branch=trunk_branch,
        )
        observation, native_stacks = await resolve_selected_native_observation(
            context=context,
            dry_run=dry_run,
            github=github,
            initial=observation,
            remote_name=target.remote.name,
            repository=target.repository,
            selected=selected,
            trunk_branch=trunk_branch,
        )
        error = _selected_observation_error(
            observation=observation,
            prepared_status=prepared_status,
            target=target,
            trunk_branch=trunk_branch,
        )
        if error is not None:
            raise CliError(error)
        plan = build_selected_convergence_plan(
            context=context,
            native_stacks=native_stacks,
            observation=observation,
            prepared_status=prepared_status,
            repository=target.repository,
        )
        _render_selected_plan(dry_run=dry_run, plan=plan)
        await _apply_selected_plan(
            context=context,
            dry_run=dry_run,
            github=github,
            plan=plan,
            target=target,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        if plan.rewrite_blocker is not None:
            raise CliError(plan.rewrite_blocker)
    return await _update_selected_reviews(
        context=context,
        dry_run=dry_run,
        plan=plan,
    )


def _selected_target(
    prepared_status: PreparedStatus,
) -> tuple[GithubTarget, tuple[LocalRevision, ...]]:
    target = prepared_status.github_target
    if not isinstance(target, GithubTarget):
        raise CliError(target.github_repository_error or "Could not resolve GitHub target.")
    selected = prepared_status.prepared.stack.revisions
    for revision in selected:
        if prepared_status.prepared.state.issues_for(revision.change_id):
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is malformed.",
                hint=t"Repair it with {ui.cmd('relink')} before syncing this path.",
            )
    return target, selected


async def _apply_selected_plan(
    *,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
    plan: SelectedConvergencePlan,
    target: GithubTarget,
    trunk_branch: str,
    trunk_commit_id: str,
) -> None:
    exact = tuple(
        landed.candidate
        for landed in plan.landed
        if landed.evidence_kind == "exact" and not landed.native
    )
    finalizer = FinalizationContext(
        command=context,
        dry_run=dry_run,
        github=github,
        remote_name=target.remote.name,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )
    exact_results = await finalize_landed_reviews(
        candidates=exact,
        finalizer=finalizer,
    )
    exact_result_iterator = iter(exact_results)
    results = tuple(
        next(exact_result_iterator)
        if landed.evidence_kind == "exact" and not landed.native
        else LandedReviewResult(
            candidate=landed.candidate,
            outcome="already_terminal",
        )
        for landed in plan.landed
    )
    rebase_revision_ids = selected_rebase_revision_ids(context=context, plan=plan)

    def retirement_blocker(candidate: LandedReviewCandidate) -> Message | None:
        if plan.rewrite_blocker is not None:
            return plan.rewrite_blocker
        return rewritten_retirement_blocker(
            candidate=candidate,
            context=context,
            plan=plan,
        )

    if not dry_run and plan.rewrite_blocker is None:
        if rebase_revision_ids:
            context.jj_client.rebase_revisions_only(
                revisions=rebase_revision_ids,
                destination=trunk_commit_id,
            )
        abandoned = tuple(
            landed.revision.commit_id
            for landed in plan.landed
            if landed.revision is not None
            and not landed.revision.immutable
            and landed.revision.commit_id == landed.candidate.submitted_baseline.commit_id
            and retirement_blocker(landed.candidate) is None
        )
        if abandoned:
            context.jj_client.abandon_revisions(abandoned)
        for survivor in plan.native_survivors:
            candidate = survivor.candidate
            if candidate.submitted_baseline.commit_id != survivor.remote_head_commit_id:
                context.state_store.advance_baseline(
                    candidate.change_id,
                    expected_identity=candidate.review_identity,
                    expected_baseline=candidate.submitted_baseline,
                    baseline=SubmittedBaseline(commit_id=survivor.remote_head_commit_id),
                )

    results = await retire_landed_reviews(
        cleanup_bookmarks=True,
        evidence={landed.candidate.change_id: landed.evidence_kind for landed in plan.landed},
        finalization_results=results,
        finalizer=finalizer,
        retirement_blocker=retirement_blocker,
        terminal_required=frozenset(
            landed.candidate.change_id for landed in plan.landed if landed.native
        ),
    )
    render_landed_results(dry_run=dry_run, results=results)


async def _update_selected_reviews(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: SelectedConvergencePlan,
) -> int:
    if plan.landed and plan.survivors and dry_run:
        console.output(
            t"Run {ui.cmd(f'jj-stack sync {plan.survivors[-1].change_id}')} to apply the rebase "
            t"and then compute updates for the remaining existing PRs."
        )
        return 0
    if not plan.reviewed_survivors:
        if plan.survivors:
            console.output("No existing reviews to update; trailing work remains local.")
        else:
            console.output("Nothing to submit: everything in this stack has merged.")
        return 0
    result = await run_submit_async(
        context=context,
        on_prepared=None,
        options=_sync_submit_options(
            dry_run=dry_run,
            revset=plan.reviewed_survivors[-1].change_id,
        ),
    )
    print_submit_result(result)
    return 0


def _selected_observation_error(
    *,
    observation: RepositoryObservation,
    prepared_status: PreparedStatus,
    target: GithubTarget,
    trunk_branch: str,
) -> Message | None:
    if (
        observation.remote != target.remote
        or observation.configured_repository != target.repository
    ):
        return "the configured Git remote changed during sync"
    if (
        observation.github_repository.full_name.casefold()
        != target.repository.full_name.casefold()
    ):
        return "GitHub no longer reports the configured repository"
    if observation.github_repository.default_branch not in (None, "", trunk_branch):
        return "GitHub no longer reports the selected trunk branch as its default"
    fetched_trunk = observation.fetched_trunk
    expected_trunk = prepared_status.prepared.stack.trunk.commit_id
    if fetched_trunk is None or fetched_trunk.commit_id != expected_trunk:
        return "fetched trunk changed during sync preparation"
    if observation.remote_trunk_target != expected_trunk:
        return "the live trunk ref moved after the fetch"
    return None


def _render_selected_plan(*, dry_run: bool, plan: SelectedConvergencePlan) -> None:
    if not plan.landed:
        console.output("No merged changes in this stack need rebasing.")
        return
    status = "Would remove" if dry_run else "Removing"
    console.output(
        t"{status} merged changes from the bottom of the stack: "
        t"{ui.join(lambda item: ui.change_id(item.candidate.change_id), plan.landed)}"
    )


def _sync_submit_options(*, dry_run: bool, revset: str) -> SubmitOptions:
    return SubmitOptions(
        descriptions=(),
        describe_with=None,
        draft_mode="default",
        dry_run=dry_run,
        edit=False,
        existing_only=True,
        labels=None,
        re_request=False,
        restart=False,
        reviewers=None,
        revset=revset,
        team_reviewers=None,
        use_bookmarks=None,
    )
