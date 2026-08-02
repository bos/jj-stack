"""Update a stack after GitHub merges, or clean up merged reviews with `sync --all`.

`sync` is the only jj-stack command that changes local history. It fetches trunk and proves
which reviewed changes reached it. It then rebases the remaining changes, updates only their
existing pull requests, and removes saved links that no local path still needs. It never creates a
pull request.

Some local states stop `sync` before it rebases:

- A remaining change has multiple visible revisions. `sync` cannot choose one.

- A merged change contains edits made after it was submitted. Removing it would discard work.

- A local change that has not merged is a parent of reviewed work that has merged. Moving the
  local change could put it before or after the merged work. `sync` will not choose for you.

- An unreviewed change sits between reviewed changes. `sync` updates existing pull requests but
  never creates the missing review.

Before rebasing, `sync` also checks the configured remote, fetched trunk, saved pull request
links, and GitHub stack membership. A missing, moved, closed, or ambiguous review stops the run
before local history changes. The message names the state to inspect or repair.

Conflicts do not prevent the local rebase. If a rebased review remains conflicted, `sync` leaves
the conflict in local history and stops before updating that pull request. Resolve the conflict
with `jj`, then run `jj-stack submit`.

Rebasing a `jj` change also rebases its descendants. This may move local work above the selected
stack, but `sync` updates pull requests only for the selected stack.

A different local path may share a merged change with the stack being synced. If that path still
uses the old local change, `sync` leaves the change and its tracking in place. It prints the other
stack to sync next. A rerun observes work already completed and continues from there.

`sync --all` never rebases or submits. It checks every locally tracked pull request and removes
tracking for those whose submitted commit is already on trunk. For stacks that need a rebase, it
prints a `jj-stack sync <head-change-id>` command instead.

Use plain `jj rebase` when trunk merely advanced and nothing in the stack merged.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.submit.command import print_selected_line, run_submit_async
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.render import print_submit_result
from jj_stack.commands.sync_global import run_global_recovery
from jj_stack.errors import CliError, ConflictedStackError, UsageError
from jj_stack.github.client import GithubClient, build_github_client
from jj_stack.github.resolution import GithubTarget, resolve_trunk_branch
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import UnsupportedStackError
from jj_stack.models.review_state import SubmittedBaseline
from jj_stack.models.stack import LocalRevision
from jj_stack.review.convergence import (
    SelectedConvergencePlan,
    build_selected_convergence_plan,
    rewritten_retirement_blocker,
)
from jj_stack.review.finish import (
    FinishContext,
    finish_exit_code,
    finish_reviews,
    render_finish_results,
    retire_reviews,
)
from jj_stack.review.github_stack_sync import resolve_selected_github_stack_observation
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_reviews,
)
from jj_stack.review.status import PreparedStatus, prepare_status, status_preparation_cli_error
from jj_stack.review.trunk_evidence import TrackedReview
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
            revset=revset,
        )


def run_stack_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    fetch_remote_state: bool = True,
    print_selected: bool = False,
    revset: str | None,
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
        observation, github_stacks = await resolve_selected_github_stack_observation(
            context=context,
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
            raise CliError(
                error,
                hint=t"Inspect the stack with {ui.cmd('jj-stack view')}, then repair it "
                t"with {ui.cmd('jj-stack relink')} or republish it with "
                t"{ui.cmd('jj-stack submit')}.",
            )
        queued_pull_numbers = tuple(
            pull_request.number
            for revision in selected
            if (pull_request := observation.reviews[revision.change_id].pull_request) is not None
            and pull_request.normalize_state().state == "open"
            and pull_request.is_queued
        )
        if queued_pull_numbers:
            console.output(
                t"Nothing to sync while the selected review is in the merge queue "
                t"({ui.join(lambda number: f'PR #{number}', queued_pull_numbers)})."
            )
            return 0
        plan = build_selected_convergence_plan(
            context=context,
            github_stacks=github_stacks,
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
        raise CliError(
            target.github_repository_error or "Could not resolve GitHub target.",
            hint=t"Point jj-stack at a GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
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
    finish_context = FinishContext(
        command=context,
        dry_run=dry_run,
        github=github,
        remote_name=target.remote.name,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )
    results = await finish_reviews(
        candidates=tuple(change.candidate for change in plan.on_trunk),
        finish=finish_context,
        skip_finish=frozenset(
            change.candidate.change_id
            for change in plan.on_trunk
            if change.evidence_kind != "exact" or change.requires_terminal_pull_request
        ),
    )
    rebase_revision_ids = (
        tuple(revision.commit_id for revision in plan.survivors) if plan.on_trunk else ()
    )

    def retirement_blocker(candidate: TrackedReview) -> Message | None:
        return rewritten_retirement_blocker(
            candidate=candidate,
            context=context,
            plan=plan,
        )

    if not dry_run:
        github_stack_survivors = plan.github_stack_survivors
        if github_stack_survivors:
            top = github_stack_survivors[-1]
            rebase_revision_ids = rebase_revision_ids[len(github_stack_survivors) :]
            replaced = tuple(
                revision.commit_id
                for revision, survivor in zip(
                    plan.survivors[: len(github_stack_survivors)],
                    github_stack_survivors,
                    strict=False,
                )
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
                    for survivor in github_stack_survivors
                ),
                expected_parent_commit_id=trunk_commit_id,
            )
        else:
            replaced = ()
            destination = trunk_commit_id
            attachment = nullcontext()
        if plan.on_trunk and not rebase_revision_ids:
            rebase_head = (
                plan.survivors[-1]
                if plan.survivors
                else next(
                    (
                        item.revision
                        for item in reversed(plan.on_trunk)
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
                change.revision.commit_id
                for change in plan.on_trunk
                if change.revision is not None
                and not change.revision.immutable
                # Convergence already refused these, but repeat the work-loss check here because
                # this is the step that actually discards commits.
                and not (
                    change.revision is not None
                    and change.revision.holds_unpublished_edit(
                        (change.candidate.submitted_baseline.commit_id,)
                    )
                )
                and retirement_blocker(change.candidate) is None
            )
            if abandoned:
                context.jj_client.abandon_revisions(abandoned)
            if github_stack_survivors:
                context.state_store.relink_reviews(
                    expected={
                        survivor.candidate.change_id: (
                            survivor.candidate.review_identity,
                            survivor.candidate.submitted_baseline,
                        )
                        for survivor in github_stack_survivors
                    },
                    replacements={
                        survivor.candidate.change_id: (
                            survivor.candidate.review_identity,
                            SubmittedBaseline(commit_id=survivor.remote_head_commit_id),
                        )
                        for survivor in github_stack_survivors
                    },
                )
    update_result = await _update_selected_reviews(
        context=context,
        dry_run=dry_run,
        plan=plan,
    )
    results = await retire_reviews(
        evidence={change.candidate.change_id: change.evidence_kind for change in plan.on_trunk},
        finish_results=results,
        finish=finish_context,
        retirement_blocker=retirement_blocker,
        terminal_required=frozenset(
            change.candidate.change_id
            for change in plan.on_trunk
            if change.requires_terminal_pull_request
        ),
    )
    render_finish_results(dry_run=dry_run, results=results)
    return finish_exit_code(base=update_result, results=results)


async def _update_selected_reviews(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: SelectedConvergencePlan,
) -> int:
    if plan.on_trunk and plan.survivors and dry_run:
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
    head_change_id = plan.reviewed_survivors[-1].change_id
    try:
        result = await run_submit_async(
            context=context,
            on_prepared=None,
            options=_sync_submit_options(
                dry_run=dry_run,
                revset=head_change_id,
            ),
        )
    except ConflictedStackError as error:
        if not plan.on_trunk:
            raise
        raise ConflictedStackError(
            error.message,
            hint=t"The local rebase is complete. Resolve the conflicts with {ui.cmd('jj')}, "
            t"then update the remaining reviews with "
            t"{ui.cmd(f'jj-stack submit {head_change_id}')}",
        ) from error
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
    if not plan.on_trunk:
        console.output("No merged changes in this stack need rebasing.")
        return
    status = "Would remove" if dry_run else "Removing"
    console.output(
        t"{status} merged changes from the bottom of the stack: "
        t"{ui.join(lambda item: ui.change_id(item.candidate.change_id), plan.on_trunk)}"
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
        reviewers=None,
        revset=revset,
        team_reviewers=None,
    )
