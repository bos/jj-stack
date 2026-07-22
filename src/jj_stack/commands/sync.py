"""Update one local stack after GitHub merges, or clean up landed PRs across the repo.

`sync <head-change-id>` fetches trunk, verifies which lower PRs landed, rebases the remaining
selected changes so they no longer depend on the old commits, and updates only PRs that already
exist. Unreviewed trailing work stays local, and other local stacks are not changed.

`sync --all` checks every locally tracked PR. When a PR's exact submitted commit is already on
trunk, it may retarget and close the PR, forget its managed local bookmark, and remove its
tracking data. It never rewrites or submits a stack. If GitHub created a different commit while
merging, it prints the `sync <head-change-id>` command needed for that stack instead.

Use `--dry-run` to fetch and preview these changes. Fetching can update jj's remembered remote
bookmark locations, but the preview does not change PRs, local commits, local bookmarks, or
tracking.
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
from jj_stack.errors import CliError, UsageError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
    resolve_github_target,
    resolve_trunk_branch,
)
from jj_stack.jj.client import JjCliArgs, JjCommandError, UnsupportedStackError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.stack import LocalRevision
from jj_stack.review.convergence import (
    SelectedConvergencePlan,
    build_selected_convergence_plan,
    dependent_path_commands,
    rewritten_retirement_blocker,
    selected_rebase_revision_ids,
)
from jj_stack.review.landed import (
    FinalizationContext,
    LandedReviewResult,
    finalize_landed_reviews,
    retire_landed_reviews,
)
from jj_stack.review.landed_evidence import (
    CommitAncestry,
    LandedReviewCandidate,
    classify_commit_ancestries,
    classify_rewritten_result,
    complete_review_candidates,
)
from jj_stack.review.observation import (
    RepositoryObservation,
    duplicate_review_claim_change_ids,
    observe_review_mutation,
)
from jj_stack.review.status import PreparedStatus, prepare_status, status_preparation_cli_error
from jj_stack.state.operation_lock import acquire_operation_lock
from jj_stack.ui import Message

HELP = "Update a stack after GitHub merges or clean up landed PRs"


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
        raise UsageError(t"Use either {ui.cmd('sync --all')} or a revision, not both.")
    context = bootstrap_context(repository=repository, cli_args=cli_args, debug=debug)
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="sync --all" if all_ else "sync",
    ):
        if all_:
            return asyncio.run(_run_global_recovery(context=context, dry_run=dry_run))
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
    exact = tuple(landed.candidate for landed in plan.landed if landed.evidence_kind == "exact")
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
        if landed.evidence_kind == "exact"
        else LandedReviewResult(
                candidate=landed.candidate,
                outcome="already_terminal",
            )
        for landed in plan.landed
    )
    rebase_revision_ids = selected_rebase_revision_ids(context=context, plan=plan)
    if plan.landed and rebase_revision_ids and not dry_run and plan.rewrite_blocker is None:
        context.jj_client.rebase_revisions_only(
            revisions=rebase_revision_ids,
            destination=trunk_commit_id,
        )

    def retirement_blocker(candidate: LandedReviewCandidate) -> Message | None:
        if plan.rewrite_blocker is not None:
            return plan.rewrite_blocker
        return rewritten_retirement_blocker(
            candidate=candidate,
            context=context,
            plan=plan,
        )

    results = await retire_landed_reviews(
        cleanup_bookmarks=True,
        evidence={landed.candidate.change_id: landed.evidence_kind for landed in plan.landed},
        finalization_results=results,
        finalizer=finalizer,
        retirement_blocker=retirement_blocker,
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
            "Existing-review update preview follows after the planned rebase is applied."
        )
        return 0
    if not plan.reviewed_survivors:
        if plan.survivors:
            console.output("No existing reviews to update; trailing work remains local.")
        else:
            console.output("Nothing to submit: everything in this stack has landed.")
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


async def _run_global_recovery(*, context: CommandContext, dry_run: bool) -> int:
    target = resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(target, GithubTarget):
        raise CliError(target.github_repository_error or "Could not resolve GitHub target.")
    context.jj_client.fetch_remote(remote=target.remote.name)
    trunk = context.jj_client.resolve_revision("trunk()")
    state = context.state_store.load()
    had_failure = bool(state.record_issues)
    for issue in state.record_issues:
        condition = "incomplete" if issue.validation_error.endswith(" is missing.") else "invalid"
        component = (
            "pull request details are"
            if issue.record_type == "review_identity"
            else "last submitted commit is"
        )
        console.warning(
            t"Skip {ui.change_id(issue.change_id)}: its saved {component} {condition}; "
            t"repair the tracking with {ui.cmd('relink')}."
        )
    all_candidates = complete_review_candidates(state)
    duplicate_change_ids = duplicate_review_claim_change_ids(state.review_identities)
    ancestry_by_commit_id = classify_commit_ancestries(
        commit_ids=tuple(candidate.submitted_baseline.commit_id for candidate in all_candidates),
        context=context,
        trunk_commit_id=trunk.commit_id,
    )
    exact_candidates = tuple(
        candidate
        for candidate in all_candidates
        if ancestry_by_commit_id[candidate.submitted_baseline.commit_id] == "on_trunk"
    )
    async with build_github_client(repository=target.repository) as github:
        repository_state = await github.get_repository()
        trunk_branch = resolve_trunk_branch(
            bookmark_states=context.jj_client.list_bookmark_states(),
            github_repository_state=repository_state,
            remote_name=target.remote.name,
            trunk_commit_id=trunk.commit_id,
        )
        pull_requests = await github.get_pull_requests_by_numbers_independently(
            pull_numbers=tuple(
                candidate.review_identity.pr_number
                for candidate in all_candidates
                if ancestry_by_commit_id[candidate.submitted_baseline.commit_id] != "on_trunk"
            )
        )
        merge_ancestry = classify_commit_ancestries(
            commit_ids=tuple(
                pull_request.merge_commit_sha
                for pull_request in pull_requests.values()
                if isinstance(pull_request, GithubPullRequest)
                and pull_request.merge_commit_sha is not None
            ),
            context=context,
            trunk_commit_id=trunk.commit_id,
        )
        for candidate in all_candidates:
            ancestry = ancestry_by_commit_id[candidate.submitted_baseline.commit_id]
            if ancestry == "on_trunk":
                continue
            pull_request = pull_requests.get(candidate.review_identity.pr_number)
            had_failure = (
                _report_global_nonexact_candidate(
                    ancestry=ancestry,
                    candidate=candidate,
                    context=context,
                    duplicate=candidate.change_id in duplicate_change_ids,
                    merge_ancestry=merge_ancestry,
                    pull_request=pull_request,
                    repository=target.repository,
                )
                or had_failure
            )
        finalizer = FinalizationContext(
            command=context,
            dry_run=dry_run,
            github=github,
            remote_name=target.remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk.commit_id,
        )
        results = await finalize_landed_reviews(
            candidates=exact_candidates,
            finalizer=finalizer,
        )
        results = await retire_landed_reviews(
            cleanup_bookmarks=True,
            evidence={candidate.change_id: "exact" for candidate in exact_candidates},
            finalization_results=results,
            finalizer=finalizer,
        )
    render_landed_results(dry_run=dry_run, results=results)
    return 1 if had_failure or any(result.outcome == "skipped" for result in results) else 0


def _report_global_nonexact_candidate(
    *,
    ancestry: CommitAncestry,
    candidate: LandedReviewCandidate,
    context: CommandContext,
    duplicate: bool,
    merge_ancestry: dict[str, CommitAncestry],
    pull_request: GithubPullRequest | GithubClientError | None,
    repository: GithubRepoAddress,
) -> bool:
    if duplicate:
        _warn_global_preserved(candidate, "another tracked change claims the same review")
        return True
    if not isinstance(pull_request, GithubPullRequest):
        reason = (
            t"could not inspect its current review: {pull_request}"
            if isinstance(pull_request, GithubClientError)
            else t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
        )
        _warn_global_preserved(candidate, reason)
        return True
    rewritten = classify_rewritten_result(
        candidate=candidate,
        merge_result_ancestry=merge_ancestry.get(pull_request.merge_commit_sha or ""),
        pull_request=pull_request,
        repository=repository,
    )
    if rewritten.state == "landed":
        try:
            commands = dependent_path_commands(
                ancestor_commit_id=candidate.submitted_baseline.commit_id,
                context=context,
            )
        except JjCommandError as error:
            _warn_global_preserved(candidate, t"could not inspect other local stacks: {error}")
            return True
        if commands is None:
            _warn_global_preserved(candidate, "no local stack is available to finish cleanup")
            return True
        console.warning(
            t"Leave {ui.change_id(candidate.change_id)} tracked: GitHub merged it as a "
            t"different commit; {commands}."
        )
        return False
    if rewritten.state in {"head_mismatch", "identity_mismatch"}:
        _warn_global_preserved(candidate, rewritten.reason or rewritten.state)
        return True
    if pull_request.normalize_state().state != "open":
        _warn_global_preserved(
            candidate,
            rewritten.reason
            or t"PR #{pull_request.number} is {pull_request.normalize_state().state} "
            t"without a result on trunk",
        )
        return True
    if ancestry == "unresolved":
        _warn_global_preserved(candidate, "the submitted commit is unavailable locally")
        return True
    return False


def _warn_global_preserved(candidate: LandedReviewCandidate, reason: Message) -> None:
    console.warning(t"Leave {ui.change_id(candidate.change_id)} tracked: {reason}.")


def render_landed_results(
    *,
    dry_run: bool,
    results: tuple[LandedReviewResult, ...],
) -> None:
    if not results:
        return
    console.output(
        "Planned cleanup for landed PRs:" if dry_run else "Applied cleanup for landed PRs:"
    )
    marker = "•" if dry_run else "✓"
    for result in results:
        candidate = result.candidate
        if result.outcome == "skipped":
            console.output(
                t"  ! leave {ui.change_id(candidate.change_id)} unchanged: {result.skip_reason}"
            )
            continue
        if result.outcome == "finalized":
            console.output(t"  {marker} finish landed PR #{candidate.review_identity.pr_number}")
        if result.forgot_bookmark:
            console.output(t"  {marker} forget {ui.bookmark(candidate.review_identity.head_ref)}")
        if result.cleanup_warning is not None:
            console.output(t"  ! cleanup still needed: {result.cleanup_warning}")
        if result.retired_tracking:
            console.output(t"  {marker} remove tracking for {ui.change_id(candidate.change_id)}")
        elif result.retirement_skip_reason is not None:
            console.output(
                t"  ! leave {ui.change_id(candidate.change_id)} tracked: "
                t"{result.retirement_skip_reason}"
            )


def _render_selected_plan(*, dry_run: bool, plan: SelectedConvergencePlan) -> None:
    if not plan.landed:
        console.output("No landed changes in this stack need rebasing.")
        return
    status = "Would remove" if dry_run else "Removing"
    console.output(
        t"{status} landed changes from the bottom of the stack: "
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
