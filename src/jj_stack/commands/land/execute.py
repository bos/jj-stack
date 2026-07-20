"""Live execution for the land command.

Execution is mutate-only: gates were checked during planning, and every
recovery path is observational. The direct-push transport moves trunk with one
leased push and then finalizes through the shared landed-review sweep; the
merge transport asks GitHub to merge the planned prefix bottom-up and reports
exactly what GitHub accepted so the caller can converge the local stack. No
durable transaction state exists: an interruption at any point is recovered by
the next `land` or `sync` from what GitHub and the jj DAG report then.
"""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.commands.sync import render_sweep_results
from jj_stack.errors import CliError, DriftError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.push_rejections import (
    classify_protected_branch_rejection,
    protected_branch_rejection_hint,
    rejection_reason_lines,
)
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.models.review_state import LandNote
from jj_stack.review.change_status import classify_review_change
from jj_stack.review.landed import (
    BookmarkCleanupPolicy,
    LandedReviewResult,
    finalize_landed_reviews,
)
from jj_stack.state.journal import OperationJournal
from jj_stack.state.store import ReviewStateStore

from .models import (
    BookmarkStateReader,
    LandAction,
    LandExecutionInputs,
    LandPlan,
    LandResult,
    LandRevision,
)
from .pull_requests import merge_landed_pull_request


def ensure_trunk_branch_matches_selected_trunk(
    *,
    client: BookmarkStateReader,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> None:
    bookmark_state = client.get_bookmark_state(trunk_branch)
    if len(bookmark_state.local_targets) > 1:
        raise CliError(
            t"Local trunk bookmark {ui.bookmark(trunk_branch)} is conflicted.",
            hint="Resolve it before landing.",
        )
    local_target = bookmark_state.local_target
    if local_target is not None and local_target != trunk_commit_id:
        inspect_command = f"jj log -r '{trunk_branch}|trunk()'"
        raise CliError(
            t"Local bookmark {ui.bookmark(trunk_branch)} points to a different "
            t"revision than {ui.revset('trunk()')}.",
            hint=(
                t"Inspect both with {ui.cmd(inspect_command)} and move "
                t"{ui.bookmark(trunk_branch)} back to {ui.revset('trunk()')} before "
                t"retrying."
            ),
        )

    remote_state = bookmark_state.remote_target(remote_name)
    review_status = classify_review_change(
        cached_change=None,
        commit_id=trunk_commit_id,
        local="present",
        pull_request_lookup=None,
        remote_state=remote_state,
    )
    if review_status.remote_branch == "absent":
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available.",
            hint="Fetch and retry.",
        )
    if review_status.remote_branch == "conflicted":
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is "
            t"conflicted.",
            hint="Resolve it before landing.",
        )
    if review_status.remote_branch_matches_commit is not True:
        remote_target = None if remote_state is None else remote_state.target
        if local_target == trunk_commit_id and remote_target is not None:
            move_command = f"jj bookmark move {trunk_branch} --to {remote_target[:12]}"
            raise CliError(
                t"Local trunk bookmark {ui.bookmark(trunk_branch)} does not match "
                t"{ui.bookmark(f'{trunk_branch}@{remote_name}')}.",
                hint=(
                    t"If an earlier {ui.cmd('land')} was interrupted before its push, move "
                    t"{ui.bookmark(trunk_branch)} back with {ui.cmd(move_command)} and rerun "
                    t"{ui.cmd('land')}; otherwise push or fetch to reconcile the trunk "
                    t"bookmarks."
                ),
            )
        raise DriftError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} moved since "
            t"the selected path was resolved.",
            condition="remote_trunk_moved",
            hint="Fetch, rebase if needed, and retry.",
        )


async def execute_land_plan(
    *,
    github_client: GithubClient,
    merge_method: str | None,
    plan: LandPlan,
    execution: LandExecutionInputs,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_commit_id: str,
    trunk_subject: str,
) -> LandResult:
    """Execute a non-dry-run land plan and return the actions that were applied."""

    client = execution.context.jj_client
    state_store = execution.context.state_store

    def land_result(
        *,
        actions: tuple[LandAction, ...],
        applied: bool,
        blocked: bool,
        merged_change_ids: tuple[str, ...] = (),
    ) -> LandResult:
        return LandResult(
            actions=actions,
            applied=applied,
            bypass_readiness=execution.bypass_readiness,
            blocked=blocked,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
            via=plan.via,
            merged_change_ids=merged_change_ids,
        )

    if plan.blocked:
        # A blocked plan is still a convergence opportunity: leftovers from an
        # interrupted land finalize here, so rerunning land keeps its promise.
        await _converge_landed_leftovers(
            execution=execution,
            github_client=github_client,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk_commit_id,
        )
        return land_result(
            actions=plan.planned_actions(),
            applied=False,
            blocked=True,
        )
    state_dir = state_store.require_writable()
    journal = OperationJournal.begin(
        state_dir,
        operation="land",
        options={
            "bypass_readiness": execution.bypass_readiness,
            "cleanup_bookmarks": execution.cleanup_bookmarks,
            "merge_method": merge_method,
            "selected_pr_number": execution.selected_pr_number,
            "via": plan.via,
        },
        resolved_scope={
            "github_repository": github_client.repository.full_name,
            "planned_change_ids": tuple(
                revision.change_id for revision in plan.planned_revisions
            ),
            "planned_commit_ids": tuple(
                revision.commit_id for revision in plan.planned_revisions
            ),
            "push_trunk": plan.push_trunk,
            "remote_name": remote_name,
            "selected_revset": selected_revset,
            "trunk_branch": trunk_branch,
        },
    )
    _write_land_note(plan=plan, state_store=state_store, trunk_branch=trunk_branch)

    actions: list[LandAction] = []
    refresh_actions = _refresh_stale_review_branches(
        client=client,
        remote_name=remote_name,
        resubmit_revisions=plan.resubmit_revisions,
        state_store=state_store,
    )
    actions.extend(refresh_actions)
    if refresh_actions:
        journal.append(
            "mutation_applied",
            {
                "actions": tuple(action.message for action in refresh_actions),
                "mutation": "refresh_review_branches",
            },
        )
    try:
        dismissed_action = await _check_post_resubmit_approvals(
            bypass_readiness=execution.bypass_readiness,
            github_client=github_client,
            resubmit_revisions=plan.resubmit_revisions,
            trunk_branch=trunk_branch,
        )
    except CliError:
        # The recheck is a read; failing it leaves only the idempotent branch
        # refresh behind, so there is nothing for a note to explain.
        _clear_land_note(state_store)
        raise
    if dismissed_action is not None:
        actions.append(dismissed_action)
        _clear_land_note(state_store)
        journal.append("completed", {"outcome": "approval_dismissed"})
        return land_result(actions=tuple(actions), applied=True, blocked=True)

    if plan.via == "push":
        return await _execute_direct_push(
            actions=actions,
            client=client,
            execution=execution,
            github_client=github_client,
            journal=journal,
            land_result=land_result,
            plan=plan,
            remote_name=remote_name,
            state_store=state_store,
            trunk_branch=trunk_branch,
        )
    await _converge_landed_leftovers(
        execution=execution,
        github_client=github_client,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )
    return await _execute_github_merges(
        actions=actions,
        github_client=github_client,
        journal=journal,
        land_result=land_result,
        merge_method=merge_method,
        plan=plan,
        state_store=state_store,
        trunk_branch=trunk_branch,
    )


async def _execute_direct_push(
    *,
    actions: list[LandAction],
    client: JjClient,
    execution: LandExecutionInputs,
    github_client: GithubClient,
    journal: OperationJournal,
    land_result,  # noqa: ANN001 - local result factory
    plan: LandPlan,
    remote_name: str,
    state_store: ReviewStateStore,
    trunk_branch: str,
) -> LandResult:
    trunk_revision = plan.planned_revisions[-1]
    journal.append(
        "planned_mutation",
        {
            "change_id": trunk_revision.change_id,
            "commit_id": trunk_revision.commit_id,
            "mutation": "push_trunk",
            "trunk_branch": trunk_branch,
        },
    )
    try:
        trunk_action = _push_trunk_bookmark(
            client=client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            trunk_revision=trunk_revision,
        )
    except JjCommandError:
        # An unclassified push failure is ambiguous — GitHub may or may not
        # have applied it — so the note stays to explain the next run.
        raise
    except CliError:
        # A classified rejection proves trunk did not move; there is nothing
        # for a later run to explain.
        _clear_land_note(state_store)
        raise
    actions.append(trunk_action)
    journal.append(
        "mutation_applied",
        {
            "action": trunk_action.message,
            "commit_id": trunk_revision.commit_id,
            "mutation": "push_trunk",
            "trunk_branch": trunk_branch,
        },
    )

    subjects = {revision.change_id: revision.subject for revision in plan.planned_revisions}
    sweep_results = await finalize_landed_reviews(
        bookmark_policy=_sweep_bookmark_policy(execution),
        github_client=github_client,
        jj_client=client,
        labels=subjects,
        order=tuple(revision.change_id for revision in plan.planned_revisions),
        state_store=state_store,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_revision.commit_id,
    )
    # Only the reviews this land was asked to land can block its exit code;
    # a straggler from an earlier interruption is advisory residue.
    planned_change_ids = {revision.change_id for revision in plan.planned_revisions}
    planned_results = tuple(
        result
        for result in sweep_results
        if result.candidate.change_id in planned_change_ids
    )
    straggler_results = tuple(
        result
        for result in sweep_results
        if result.candidate.change_id not in planned_change_ids
    )
    sweep_actions, any_skipped = render_landed_sweep_actions(
        results=planned_results,
        subjects=subjects,
    )
    actions.extend(sweep_actions)
    render_sweep_results(dry_run=False, results=straggler_results)
    _clear_land_note(state_store)
    journal.append(
        "completed",
        {
            "retired_change_ids": tuple(
                result.candidate.change_id for result in sweep_results if result.retired
            ),
        },
    )
    if any_skipped:
        return land_result(actions=tuple(actions), applied=True, blocked=True)
    return land_result(
        actions=plan.completed_actions(actions=tuple(actions)),
        applied=True,
        blocked=False,
    )


async def _execute_github_merges(
    *,
    actions: list[LandAction],
    github_client: GithubClient,
    journal: OperationJournal,
    land_result,  # noqa: ANN001 - local result factory
    merge_method: str | None,
    plan: LandPlan,
    state_store: ReviewStateStore,
    trunk_branch: str,
) -> LandResult:
    if merge_method is None:
        raise AssertionError("The merge transport requires a resolved merge method.")
    merged_change_ids: list[str] = []
    blocked_action: LandAction | None = None
    for landed_revision in plan.planned_revisions:
        console.output(
            t"Merging PR #{landed_revision.pull_request_number} for "
            t"{landed_revision.subject} "
            t"{ui.change_id(landed_revision.change_id)}..."
        )
        journal.append(
            "planned_mutation",
            {
                "change_id": landed_revision.change_id,
                "mutation": "merge_pull_request",
                "pull_request_number": landed_revision.pull_request_number,
            },
        )
        final_pull_request, blocked = await merge_landed_pull_request(
            github_client=github_client,
            landed_revision=landed_revision,
            merge_method=merge_method,
            trunk_branch=trunk_branch,
        )
        if blocked is not None or final_pull_request is None:
            blocked_action = blocked
            break
        journal.append(
            "mutation_applied",
            {
                "change_id": landed_revision.change_id,
                "mutation": "merge_pull_request",
                "pull_request_number": landed_revision.pull_request_number,
            },
        )
        actions.append(
            LandAction(
                kind="pull request",
                body=t"merge PR #{landed_revision.pull_request_number} into "
                t"{ui.bookmark(trunk_branch)} on GitHub for "
                t"{landed_revision.subject} "
                t"{ui.change_id(landed_revision.change_id)}",
                status="applied",
            )
        )
        merged_change_ids.append(landed_revision.change_id)
    if blocked_action is not None:
        actions.append(blocked_action)
    _clear_land_note(state_store)
    journal.append("completed", {"merged_change_ids": tuple(merged_change_ids)})
    blocked = len(merged_change_ids) != len(plan.planned_revisions)
    return land_result(
        actions=(
            tuple(actions)
            if blocked
            else plan.completed_actions(actions=tuple(actions))
        ),
        applied=True,
        blocked=blocked,
        merged_change_ids=tuple(merged_change_ids),
    )


def _sweep_bookmark_policy(execution: LandExecutionInputs) -> BookmarkCleanupPolicy:
    return BookmarkCleanupPolicy(
        cleanup_bookmarks=execution.cleanup_bookmarks,
        cleanup_user_bookmarks=execution.context.config.cleanup_user_bookmarks,
        prefix=execution.context.config.bookmark_prefix,
    )


async def _converge_landed_leftovers(
    *,
    execution: LandExecutionInputs,
    github_client: GithubClient,
    trunk_branch: str,
    trunk_commit_id: str,
) -> None:
    """Finalize and retire reviews an earlier interrupted land left behind."""

    results = await finalize_landed_reviews(
        bookmark_policy=_sweep_bookmark_policy(execution),
        github_client=github_client,
        jj_client=execution.context.jj_client,
        state_store=execution.context.state_store,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )
    render_sweep_results(dry_run=False, results=results)


def render_landed_sweep_actions(
    *,
    results: tuple[LandedReviewResult, ...],
    subjects: dict[str, str] | None = None,
) -> tuple[tuple[LandAction, ...], bool]:
    """Render sweep results as land actions; True when anything was skipped."""

    labels = subjects or {}
    actions: list[LandAction] = []
    any_skipped = False
    for result in results:
        candidate = result.candidate
        subject = labels.get(candidate.change_id)
        label = (
            t"{subject} {ui.change_id(candidate.change_id)}"
            if subject is not None
            else t"{ui.change_id(candidate.change_id)}"
        )
        if result.outcome == "skipped":
            any_skipped = True
            actions.append(
                LandAction(
                    kind="pull request",
                    body=t"finalizing landed {label} skipped: {result.skip_reason}; "
                    t"inspect with {ui.cmd('view --fetch')} and rerun {ui.cmd('sync')}",
                    status="blocked",
                )
            )
            continue
        if result.outcome == "finalized":
            actions.append(
                LandAction(
                    kind="pull request",
                    body=t"finalize PR #{candidate.pull_request_number} for {label}",
                    status="applied",
                )
            )
        if result.forgot_bookmark:
            actions.append(
                LandAction(
                    kind="local bookmark",
                    body=t"forget {ui.bookmark(candidate.bookmark)} for {label}",
                    status="applied",
                )
            )
        actions.append(
            LandAction(
                kind="tracking",
                body=t"remove tracking for landed {label}",
                status="applied",
            )
        )
    return tuple(actions), any_skipped


def _write_land_note(
    *,
    plan: LandPlan,
    state_store: ReviewStateStore,
    trunk_branch: str,
) -> None:
    """Record what this land is about to ask GitHub, for messaging only.

    The note never gates behavior: it exists so the next command can explain an
    interrupted land instead of changing state silently, and it is cleared as
    soon as any run finishes with full knowledge of the outcome.
    """

    state = state_store.load()
    state_store.save(
        state.model_copy(
            update={
                "land_note": LandNote(
                    pull_request_numbers=tuple(
                        revision.pull_request_number
                        for revision in plan.planned_revisions
                    ),
                    trunk_branch=trunk_branch,
                    via=plan.via,
                )
            }
        )
    )


def _clear_land_note(state_store: ReviewStateStore) -> None:
    state = state_store.load()
    if state.land_note is None:
        return
    state_store.save(state.model_copy(update={"land_note": None}))


def _refresh_stale_review_branches(
    *,
    client: JjClient,
    remote_name: str,
    resubmit_revisions: tuple[LandRevision, ...],
    state_store: ReviewStateStore,
) -> tuple[LandAction, ...]:
    """Re-push diff-equivalent review branches so PRs match the local commits.

    Re-pushing the same commit is idempotent, and the refreshed submitted
    baseline is recorded from what was just observed to be pushed; there is no
    checkpoint to reconcile later.
    """

    if not resubmit_revisions:
        return ()

    console.output(
        t"Refreshing {len(resubmit_revisions)} review "
        t"{'branch' if len(resubmit_revisions) == 1 else 'branches'} "
        t"to match the rebased local stack..."
    )
    for resubmit_revision in resubmit_revisions:
        client.set_bookmark(
            resubmit_revision.bookmark,
            resubmit_revision.commit_id,
            allow_backwards=True,
        )
    client.push_bookmarks(
        remote=remote_name,
        bookmarks=tuple(revision.bookmark for revision in resubmit_revisions),
    )
    state = state_store.load()
    next_changes = dict(state.changes)
    for revision in resubmit_revisions:
        cached_change = next_changes.get(revision.change_id)
        if cached_change is not None:
            next_changes[revision.change_id] = cached_change.model_copy(
                update={"last_submitted_commit_id": revision.commit_id}
            )
    state_store.save(state.model_copy(update={"changes": next_changes}))
    return tuple(
        LandAction(
            kind="review branch",
            body=t"refresh {ui.bookmark(revision.bookmark)} to "
            t"{revision.subject} "
            t"{ui.change_id(revision.change_id)}",
            status="applied",
        )
        for revision in resubmit_revisions
    )


def _push_trunk_bookmark(
    *,
    client: JjClient,
    remote_name: str,
    trunk_branch: str,
    trunk_revision: LandRevision,
) -> LandAction:
    original_trunk_target = client.get_bookmark_state(trunk_branch).local_target
    try:
        client.set_bookmark(trunk_branch, trunk_revision.commit_id)
        client.push_bookmarks(
            remote=remote_name,
            bookmarks=(trunk_branch,),
        )
    except JjCommandError as error:
        _restore_local_trunk_bookmark(
            client=client,
            original_target=original_trunk_target,
            trunk_branch=trunk_branch,
        )
        rejection_reason = classify_protected_branch_rejection(str(error))
        if rejection_reason is None:
            raise
        raise CliError(
            t"GitHub rejected the {ui.bookmark(trunk_branch)} push as a "
            t"protected-branch violation:\n"
            t"{rejection_reason_lines(str(error))}",
            hint=protected_branch_rejection_hint(rejection_reason),
        ) from error
    except BaseException:
        _restore_local_trunk_bookmark(
            client=client,
            original_target=original_trunk_target,
            trunk_branch=trunk_branch,
        )
        raise
    return LandAction(
        kind="trunk",
        body=t"push {ui.bookmark(trunk_branch)} to "
        t"{trunk_revision.subject} "
        t"{ui.change_id(trunk_revision.change_id)}",
        status="applied",
    )


def _restore_local_trunk_bookmark(
    *,
    client: JjClient,
    original_target: str | None,
    trunk_branch: str,
) -> None:
    if original_target is None:
        client.forget_bookmarks((trunk_branch,))
        return
    client.set_bookmark(trunk_branch, original_target, allow_backwards=True)


async def _check_post_resubmit_approvals(
    *,
    bypass_readiness: bool,
    github_client: GithubClient,
    resubmit_revisions: tuple[LandRevision, ...],
    trunk_branch: str,
) -> LandAction | None:
    """Return a blocking action if the resubmit push dismissed any approval."""

    if bypass_readiness or not resubmit_revisions:
        return None
    try:
        decisions = await github_client.get_review_decisions_by_pull_request_numbers(
            pull_numbers=tuple(
                revision.pull_request_number for revision in resubmit_revisions
            ),
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not re-check PR review decisions after refreshing review branches"
        ) from error
    for revision in resubmit_revisions:
        decision = decisions.get(revision.pull_request_number)
        if decision != "approved":
            return LandAction(
                kind="boundary",
                body=t"before landing because refreshing "
                t"{ui.bookmark(revision.bookmark)} dismissed the approval on "
                t"PR #{revision.pull_request_number}; request re-review and rerun "
                t"{ui.cmd('land')}",
                status="blocked",
            )
    return None
