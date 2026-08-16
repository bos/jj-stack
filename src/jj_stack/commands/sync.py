"""Apply completed GitHub merges to a local stack and refresh the pull requests that remain.

`sync` fetches trunk and determines which reviewed changes have reached it. It then rebases the
remaining changes, updates only their existing pull requests, and removes review branches,
comments, and saved links for merged pull requests. If GitHub used rebase merging, `sync`
verifies the new commits, applies them locally, and restores the original `jj` change IDs. A
completed direct `merge` performs the same update. Neither command creates a pull request.

`sync` stops before rebasing in any of these cases:

- A remaining change has multiple visible revisions. `sync` cannot choose one.

- A merged change contains edits made after it was submitted. Removing it would discard work.

- A local change that has not merged is a parent of reviewed work that has merged. Moving the
  local change could put it before or after the merged work. `sync` will not choose for you.

- An unreviewed change sits between reviewed changes. `sync` updates existing pull requests but
  never creates the missing pull request.

Before rebasing, `sync` also checks saved pull request links and GitHub stack membership. A
missing or closed pull request, a changed stack relationship, or ambiguous tracking stops the
command before it changes local history. The error identifies what needs attention.

Conflicts do not prevent the local rebase. If a rebased change remains conflicted, `sync` leaves
the conflict in local history and stops before updating that pull request. Resolve the conflict
with `jj`, then run `jj-stack submit`.

Rebasing a `jj` change also rebases its descendants. This may move local work above the selected
stack, but `sync` updates pull requests only for the selected stack.

Another local stack may share a merged change with the stack being synced. If that stack still
uses the old local change, `sync` leaves the change in place and prints the other stack to sync
next. Rerunning `sync` skips completed work and continues.

`sync --all` checks every pull request known to `jj-stack`. It updates each affected local stack
in turn and also cleans up pull requests whose submitted commits are on trunk even when their
local changes are gone.

Use plain `jj rebase` when trunk merely advanced and GitHub did not rewrite the commits.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.cleanup.command import cleanup_tracked_reviews
from jj_stack.commands.submit.command import run_submit_async
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.render import print_selected_line, print_submit_result
from jj_stack.commands.sync_global import run_global_recovery
from jj_stack.errors import (
    CliError,
    ConflictedStackError,
    UsageError,
    error_hint,
    error_message,
    resolve_exit_code,
)
from jj_stack.github.client import GithubClient, build_github_client
from jj_stack.github.resolution import GithubTarget, resolve_trunk_branch
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import UnsupportedStackError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import SubmittedBaseline
from jj_stack.models.stack import LocalRevision
from jj_stack.review.convergence import (
    SelectedConvergencePlan,
    build_selected_convergence_plan,
    rewritten_removal_blocker,
)
from jj_stack.review.finish import (
    FinishContext,
    ReviewFinishResult,
    finish_reviews,
    render_finish_results,
)
from jj_stack.review.github_stack_rebase import recover_github_stack_rebase
from jj_stack.review.github_stack_sync import (
    observe_github_stacks,
    resolve_selected_github_stack_observation,
)
from jj_stack.review.observation import RepositoryObservation, observe_reviews
from jj_stack.review.status import PreparedStatus, prepare_status, status_preparation_cli_error
from jj_stack.review.trunk_evidence import TrackedReview
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Apply completed GitHub merges locally and refresh the pull requests that remain"


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
            return _run_all_convergence(context=context, dry_run=dry_run)
        return run_stack_convergence(
            context=context,
            dry_run=dry_run,
            print_selected=revset is None,
            revset=revset,
        )


def _run_all_convergence(*, context: CommandContext, dry_run: bool) -> int:
    result = asyncio.run(run_global_recovery(context=context, dry_run=dry_run))
    exit_code = result.exit_code
    for change_id in result.sync_change_ids:
        console.output(t"Syncing local stack {ui.change_id(change_id[:8])}:")
        try:
            stack_exit_code = run_stack_convergence(
                context=context,
                dry_run=dry_run,
                fetch_remote_state=False,
                revset=change_id,
                trunk_branch=result.trunk_branch,
            )
        except CliError as error:
            console.error(
                t"Could not sync local stack {ui.change_id(change_id[:8])}: "
                t"{error_message(error)}"
            )
            if hint := error_hint(error):
                console.stderr_output(
                    (ui.semantic_text("Hint: ", "hint", "heading"), hint),
                    soft_wrap=True,
                )
            if exit_code == 0:
                exit_code = resolve_exit_code(error)
        else:
            if exit_code == 0:
                exit_code = stack_exit_code
    return exit_code


def run_stack_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    fetch_remote_state: bool = True,
    print_selected: bool = False,
    revset: str | None,
    trunk_branch: str | None = None,
) -> int:
    try:
        prepared_status = prepare_status(
            context=context,
            fetch_remote_state=fetch_remote_state,
            observe_remote_targets=False,
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
            trunk_branch=trunk_branch,
        )
    )


async def _run_selected_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    prepared_status: PreparedStatus,
    trunk_branch: str | None,
) -> int:
    prepared = prepared_status.prepared
    target, selected = _selected_target(prepared_status)
    if not selected:
        console.output("Nothing to sync: the selected revision is already on trunk.")
        return 0
    async with build_github_client(repository=target.repository) as github:
        observation, observed_stacks = await asyncio.gather(
            observe_reviews(
                change_ids=tuple(revision.change_id for revision in selected),
                context=context,
                github_client=github,
                include_remote_targets=False,
                remote_name=target.remote.name,
            ),
            observe_github_stacks(github=github),
        )
        if queued_pull_numbers := _queued_pull_numbers(observation, selected):
            _render_queued_sync(queued_pull_numbers)
            return 0
        observation, github_stacks, complete = await resolve_selected_github_stack_observation(
            context=context,
            github=github,
            initial=observation,
            remote_name=target.remote.name,
            repository=target.repository,
            selected=selected,
            stacks=observed_stacks,
        )
        if queued_pull_numbers := _queued_pull_numbers(observation, selected):
            _render_queued_sync(queued_pull_numbers)
            return 0
        if not complete:
            console.output("No merged changes in this stack need rebasing.")
            return 0
        repository_state = observation.github_repository
        if repository_state is None:
            raise AssertionError("Sync observation requires GitHub repository state.")
        if trunk_branch is None:
            trunk_branch, _trunk_targets = resolve_trunk_branch(
                client=prepared.client,
                github_repository_state=repository_state,
                remote=target.remote,
                trunk_commit_id=prepared.stack.trunk.commit_id,
            )
        plan = build_selected_convergence_plan(
            context=context,
            github_stacks=github_stacks,
            observation=observation,
            prepared_status=prepared_status,
            repository=target.repository,
            trunk_branch=trunk_branch,
        )
        _render_selected_plan(dry_run=dry_run, plan=plan)
        return await _apply_selected_plan(
            context=context,
            dry_run=dry_run,
            github=github,
            plan=plan,
            pull_requests={
                pull_request.number: pull_request
                for change in plan.on_trunk
                if (pull_request := observation.reviews[change.candidate.change_id].pull_request)
                is not None
            },
            target=target,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )


def _render_queued_sync(pull_numbers: tuple[int, ...]) -> None:
    console.output(
        t"Nothing to sync while the selected review is in the merge queue "
        t"({ui.join(lambda number: f'PR #{number}', pull_numbers)})."
    )


def _queued_pull_numbers(
    observation: RepositoryObservation,
    selected: tuple[LocalRevision, ...],
) -> tuple[int, ...]:
    return tuple(
        pull_request.number
        for revision in selected
        if (pull_request := observation.reviews[revision.change_id].pull_request) is not None
        and pull_request.normalize_state().state == "open"
        and pull_request.is_queued
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
    return target, prepared_status.prepared.stack.revisions


async def _apply_selected_plan(
    *,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
    plan: SelectedConvergencePlan,
    pull_requests: dict[int, GithubPullRequest],
    target: GithubTarget,
    trunk_branch: str,
    trunk_commit_id: str,
) -> int:
    rewrite = plan.github_stack_rewrite
    if rewrite is not None and rewrite.mode == "rebase":
        return recover_github_stack_rebase(
            context=context,
            dry_run=dry_run,
            plan=plan,
            remote_name=target.remote.name,
            rewrite=rewrite,
            trunk_commit_id=trunk_commit_id,
        )
    finish_context = FinishContext(
        dry_run=dry_run,
        github=github,
        trunk_branch=trunk_branch,
    )
    results = await finish_reviews(
        candidates=tuple(change.candidate for change in plan.on_trunk),
        finish=finish_context,
        pull_requests=pull_requests,
        skip_finish=frozenset(
            change.candidate.change_id
            for change in plan.on_trunk
            if change.evidence_kind != "exact" or change.requires_terminal_pull_request
        ),
    )
    rebase_revision_ids = (
        tuple(revision.commit_id for revision in plan.survivors) if plan.on_trunk else ()
    )

    if not dry_run:
        github_stack_survivors = rewrite.active if rewrite is not None else ()
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
                and rewritten_removal_blocker(
                    candidate=change.candidate,
                    context=context,
                    plan=plan,
                )
                is None
            )
            if abandoned:
                context.jj_client.abandon_revisions(abandoned)
            if github_stack_survivors:
                context.state_store.relink_reviews(
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
    render_finish_results(dry_run=dry_run, results=results)
    return await _cleanup_reconciled_reviews(
        context=context,
        dry_run=dry_run,
        finish_results=results,
        github=github,
        plan=plan,
        target=target,
        update_result=update_result,
    )


async def _cleanup_reconciled_reviews(
    *,
    context: CommandContext,
    dry_run: bool,
    finish_results: tuple[ReviewFinishResult, ...],
    github: GithubClient,
    plan: SelectedConvergencePlan,
    target: GithubTarget,
    update_result: int,
) -> int:
    """Clean artifacts only after the selected local update has succeeded."""

    if update_result != 0:
        return update_result
    cleanup_candidates: list[TrackedReview] = []
    for result in finish_results:
        if result.outcome == "skipped":
            continue
        blocker = rewritten_removal_blocker(
            candidate=result.candidate,
            context=context,
            plan=plan,
        )
        if blocker is not None:
            console.output(
                t"  ! kept PR #{result.candidate.review_identity.pr_number} and its review "
                t"branch for {ui.change_id(result.candidate.change_id)}: {blocker}"
            )
            continue
        cleanup_candidates.append(result.candidate)
    review_identities = context.state_store.load().review_identities
    cleanup_result = await cleanup_tracked_reviews(
        change_ids=tuple(candidate.change_id for candidate in cleanup_candidates),
        context=context,
        dry_run=dry_run,
        github_client=github,
        github_target=target,
        planned_detached_dependents=frozenset(
            identity.pr_number
            for revision in plan.reviewed_survivors
            if (identity := review_identities.get(revision.change_id)) is not None
        ),
        planned_local_removals=frozenset(candidate.change_id for candidate in cleanup_candidates),
    )
    return 1 if any(action.status == "blocked" for action in cleanup_result.actions) else 0


async def _update_selected_reviews(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: SelectedConvergencePlan,
) -> int:
    if not plan.on_trunk:
        return 0
    if plan.survivors and dry_run:
        console.output(
            t"Run {ui.cmd(f'jj-stack sync {plan.survivors[-1].change_id}')} to apply the rebase "
            t"and then compute updates for the remaining existing PRs."
        )
        return 0
    if not plan.reviewed_survivors:
        if plan.survivors:
            console.output("No existing reviews to update; trailing work remains local.")
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


def _render_selected_plan(*, dry_run: bool, plan: SelectedConvergencePlan) -> None:
    rewrite = plan.github_stack_rewrite
    if rewrite is not None and rewrite.mode == "rebase":
        action = "Would restore" if dry_run else "Restoring"
        console.output(f"{action} the stack's jj change IDs after GitHub rebased it.")
        return
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
