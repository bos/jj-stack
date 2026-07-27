"""Update a stack after GitHub merges, or clean up merged reviews with `sync --all`."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._fetch_isolation import report_fetch_isolation
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
)
from jj_stack.review.landed import (
    FinalizationContext,
    finalize_landed_reviews,
    landed_exit_code,
    render_landed_results,
    retire_landed_reviews,
)
from jj_stack.review.landed_evidence import LandedReviewCandidate, holds_unpublished_edit
from jj_stack.review.native_sync import resolve_selected_native_observation
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_reviews,
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
            dry_run=dry_run,
            fetch_remote_state=fetch_remote_state,
            on_fetch_isolation_change=report_fetch_isolation,
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
        trunk_branch, _trunk_targets = resolve_trunk_branch(
            client=prepared.client,
            github_repository_state=repository_state,
            remote=target.remote,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        observation = await observe_reviews(
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
        return await _apply_selected_plan(
            context=context,
            dry_run=dry_run,
            github=github,
            plan=plan,
            target=target,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
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
) -> int:
    finalizer = FinalizationContext(
        command=context,
        dry_run=dry_run,
        github=github,
        remote_name=target.remote.name,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )
    results = await finalize_landed_reviews(
        candidates=tuple(landed.candidate for landed in plan.landed),
        finalizer=finalizer,
        skip_finalization=frozenset(
            landed.candidate.change_id
            for landed in plan.landed
            if landed.evidence_kind != "exact" or landed.native
        ),
    )
    rebase_revision_ids = (
        tuple(revision.commit_id for revision in plan.survivors) if plan.landed else ()
    )

    def retirement_blocker(candidate: LandedReviewCandidate) -> Message | None:
        return rewritten_retirement_blocker(
            candidate=candidate,
            context=context,
            plan=plan,
        )

    if not dry_run:
        native = plan.native_survivors
        if native:
            top = native[-1]
            rebase_revision_ids = rebase_revision_ids[len(native) :]
            replaced = tuple(
                revision.commit_id
                for revision, survivor in zip(plan.survivors[: len(native)], native, strict=False)
                if revision.commit_id != survivor.remote_head_commit_id
            )
            destination = top.remote_head_commit_id
            attachment = context.jj_client.import_remote_review_ref(
                remote=target.remote.name,
                branch=top.candidate.review_identity.head_ref,
                expected_target=destination,
                expected_change_id=top.candidate.change_id,
                expected_chain=tuple(
                    (
                        survivor.candidate.review_identity.head_ref,
                        survivor.remote_head_commit_id,
                        survivor.candidate.change_id,
                    )
                    for survivor in native
                ),
                expected_parent_commit_id=trunk_commit_id,
            )
        else:
            replaced = ()
            destination = trunk_commit_id
            attachment = nullcontext()
        if plan.landed and not rebase_revision_ids:
            rebase_head = (
                plan.survivors[-1]
                if plan.survivors
                else next(
                    (
                        item.revision
                        for item in reversed(plan.landed)
                        if item.revision is not None
                    ),
                    None,
                )
            )
            if rebase_head is not None:
                rebase_revision_ids = tuple(
                    revision.commit_id
                    for revision in context.jj_client.query_descendant_revisions(
                        (rebase_head.commit_id,)
                    )
                    if revision.is_working_copy
                    and revision.empty
                    and revision.parents == (rebase_head.commit_id,)
                )
        with attachment:
            if rebase_revision_ids:
                context.jj_client.rebase_revisions_only(
                    revisions=rebase_revision_ids,
                    destination=destination,
                )
            if replaced:
                context.jj_client.abandon_revisions(replaced)
            abandoned = tuple(
                landed.revision.commit_id
                for landed in plan.landed
                if landed.revision is not None
                and not landed.revision.immutable
                # Convergence already refused these, but ask the one authority again here:
                # this is the step that actually discards commits.
                and not holds_unpublished_edit(
                    published_commit_ids=(landed.candidate.submitted_baseline.commit_id,),
                    revision=landed.revision,
                )
                and retirement_blocker(landed.candidate) is None
            )
            if abandoned:
                context.jj_client.abandon_revisions(abandoned)
            if native:
                context.state_store.relink_reviews(
                    expected={
                        survivor.candidate.change_id: (
                            survivor.candidate.review_identity,
                            survivor.candidate.submitted_baseline,
                        )
                        for survivor in native
                    },
                    replacements={
                        survivor.candidate.change_id: (
                            survivor.candidate.review_identity,
                            SubmittedBaseline(commit_id=survivor.remote_head_commit_id),
                        )
                        for survivor in native
                    },
                )
    update_result = await _update_selected_reviews(
        context=context,
        dry_run=dry_run,
        plan=plan,
    )
    results = await retire_landed_reviews(
        evidence={landed.candidate.change_id: landed.evidence_kind for landed in plan.landed},
        finalization_results=results,
        finalizer=finalizer,
        retirement_blocker=retirement_blocker,
        terminal_required=frozenset(
            landed.candidate.change_id for landed in plan.landed if landed.native
        ),
    )
    render_landed_results(dry_run=dry_run, results=results)
    return landed_exit_code(base=update_result, results=results)


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
    github_repository = observation.github_repository
    assert github_repository is not None
    if github_repository.full_name.casefold() != target.repository.full_name.casefold():
        return "GitHub no longer reports the configured repository"
    if github_repository.default_branch not in (None, "", trunk_branch):
        return "GitHub no longer reports the selected trunk branch as its default"
    expected_trunk = prepared_status.prepared.stack.trunk.commit_id
    if observation.fetched_trunk_commit_id != expected_trunk:
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
    )
