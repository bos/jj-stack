"""Live execution for the land command."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.push_rejections import (
    classify_protected_branch_rejection,
    protected_branch_rejection_hint,
    rejection_reason_lines,
)
from jj_stack.github.stack_comments import StackCommentKind, delete_stack_comment
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import CachedChange
from jj_stack.review.change_status import classify_review_change
from jj_stack.state.journal import OperationJournal

from .models import (
    BookmarkStateReader,
    LandAction,
    LandMutationRun,
    LandPlan,
    LandResult,
    LandRevision,
    PreparedLand,
    ReviewBookmarkCleanupPlan,
    landed_tracking_retire_body,
)


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
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} moved since "
            t"the selected path was resolved.",
            hint="Fetch, rebase if needed, and retry.",
        )


async def execute_land_plan(
    *,
    bookmark_cleanup_plans: tuple[ReviewBookmarkCleanupPlan, ...],
    github_client: GithubClient,
    merge_method: str | None,
    plan: LandPlan,
    prepared_land: PreparedLand,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_subject: str,
) -> LandResult:
    """Execute a non-dry-run land plan and return the actions that were applied."""

    prepared_status = prepared_land.prepared_status
    prepared = prepared_status.prepared

    def land_result(
        *,
        actions: tuple[LandAction, ...],
        applied: bool,
        blocked: bool,
    ) -> LandResult:
        return LandResult(
            actions=actions,
            applied=applied,
            bypass_readiness=prepared_land.bypass_readiness,
            blocked=blocked,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
            via=plan.via,
        )

    execution_plan = plan
    if execution_plan.blocked:
        return land_result(
            actions=execution_plan.planned_actions(),
            applied=False,
            blocked=True,
        )
    state_dir = prepared.state_store.require_writable()
    journal = OperationJournal.begin(
        state_dir,
        durable=execution_plan.push_trunk,
        operation="land",
        options={
            "bypass_readiness": prepared_land.bypass_readiness,
            "cleanup_bookmarks": prepared_land.cleanup_bookmarks,
            "merge_method": merge_method,
            "selected_pr_number": prepared_land.selected_pr_number,
            "via": plan.via,
        },
        resolved_scope={
            "github_repository": github_client.repository.full_name,
            "landed_change_ids": tuple(
                revision.change_id for revision in execution_plan.planned_revisions
            ),
            "landed_commit_id": (
                execution_plan.planned_revisions[-1].commit_id
                if execution_plan.planned_revisions
                else prepared.stack.trunk.commit_id
            ),
            "ordered_change_ids": tuple(
                prepared_revision.revision.change_id
                for prepared_revision in prepared_status.prepared.status_revisions
            ),
            "ordered_commit_ids": tuple(
                prepared_revision.revision.commit_id
                for prepared_revision in prepared_status.prepared.status_revisions
            ),
            "planned_revisions": tuple(
                {
                    "bookmark": revision.bookmark,
                    "bookmark_managed": revision.bookmark_managed,
                    "change_id": revision.change_id,
                    "commit_id": revision.commit_id,
                    "pull_request_number": revision.pull_request_number,
                    "subject": revision.subject,
                }
                for revision in execution_plan.planned_revisions
            ),
            "push_trunk": execution_plan.push_trunk,
            "remote_name": remote_name,
            "selected_revset": selected_revset,
            "trunk_branch": trunk_branch,
        },
    )

    state = prepared.state_store.load()
    mutation_run = LandMutationRun(
        state=state,
        state_changes=dict(state.changes),
        state_store=prepared.state_store,
    )

    actions: list[LandAction] = []
    bookmark_cleanup_by_change_id = {
        cleanup_plan.change_id: cleanup_plan for cleanup_plan in bookmark_cleanup_plans
    }
    trunk_transition_blocked = await _apply_trunk_transition(
        actions=actions,
        client=prepared.client,
        execution_plan=execution_plan,
        github_client=github_client,
        journal=journal,
        prepared_land=prepared_land,
        remote_name=remote_name,
        trunk_branch=trunk_branch,
    )
    if trunk_transition_blocked:
        return land_result(
            actions=tuple(actions),
            applied=True,
            blocked=True,
        )
    finalized_change_ids = await _finalize_planned_revisions(
        actions=actions,
        bookmark_cleanup_by_change_id=bookmark_cleanup_by_change_id,
        client=prepared.client,
        execution_plan=execution_plan,
        github_client=github_client,
        journal=journal,
        merge_method=merge_method,
        mutation_run=mutation_run,
        trunk_branch=trunk_branch,
    )
    finalize_blocked = len(finalized_change_ids) != len(execution_plan.planned_revisions)
    if not finalize_blocked and execution_plan.via == "push":
        actions.extend(
            _retire_finalized_tracking(
                finalized_revisions=execution_plan.planned_revisions,
                journal=journal,
                mutation_run=mutation_run,
            )
        )
    journal.append(
        "completed",
        {"completed_change_ids": finalized_change_ids},
        durable=execution_plan.push_trunk,
    )
    if finalize_blocked:
        return land_result(
            actions=tuple(actions),
            applied=True,
            blocked=True,
        )
    return land_result(
        actions=execution_plan.completed_actions(actions=tuple(actions)),
        applied=True,
        blocked=False,
    )


async def _apply_trunk_transition(
    *,
    actions: list[LandAction],
    client: JjClient,
    execution_plan: LandPlan,
    github_client: GithubClient,
    journal: OperationJournal,
    prepared_land: PreparedLand,
    remote_name: str,
    trunk_branch: str,
) -> bool:
    if not execution_plan.planned_revisions:
        return False

    if execution_plan.resubmit_revisions:
        journal.append(
            "planned_mutation",
            {
                "change_ids": tuple(
                    revision.change_id for revision in execution_plan.resubmit_revisions
                ),
                "mutation": "refresh_review_branches",
            },
        )
    if execution_plan.repair_local_trunk_commit_id is not None:
        trunk_commit_id = execution_plan.repair_local_trunk_commit_id
        journal.append(
            "planned_mutation",
            {
                "commit_id": trunk_commit_id,
                "mutation": "repair_local_trunk",
                "trunk_branch": trunk_branch,
            },
        )
        client.set_bookmark(trunk_branch, trunk_commit_id, allow_backwards=True)
        action = LandAction(
            kind="local trunk",
            body=t"move {ui.bookmark(trunk_branch)} to the current "
            t"{ui.revset('trunk()')} after the interrupted push",
            status="applied",
        )
        actions.append(action)
        journal.append(
            "mutation_applied",
            {
                "action": action.message,
                "commit_id": trunk_commit_id,
                "mutation": "repair_local_trunk",
                "trunk_branch": trunk_branch,
            },
        )
    refresh_actions, dismissed_action = await _refresh_rebased_review_branches(
        bypass_readiness=prepared_land.bypass_readiness,
        client=client,
        github_client=github_client,
        resubmit_revisions=execution_plan.resubmit_revisions,
        remote_name=remote_name,
        trunk_branch=trunk_branch,
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
    if dismissed_action is not None:
        actions.append(dismissed_action)
        return True
    if not execution_plan.push_trunk:
        # The merge transport lands each PR through GitHub instead of moving
        # trunk itself.
        return False

    trunk_revision = execution_plan.planned_revisions[-1]
    journal.append(
        "planned_mutation",
        {
            "change_id": trunk_revision.change_id,
            "commit_id": trunk_revision.commit_id,
            "mutation": "push_trunk",
            "trunk_branch": trunk_branch,
        },
    )
    trunk_action = _push_trunk_bookmark(
        client=client,
        remote_name=remote_name,
        trunk_branch=trunk_branch,
        trunk_revision=trunk_revision,
    )
    journal.append(
        "mutation_applied",
        {
            "action": trunk_action.message,
            "change_id": trunk_revision.change_id,
            "commit_id": trunk_revision.commit_id,
            "mutation": "push_trunk",
            "trunk_branch": trunk_branch,
        },
        durable=True,
    )
    actions.append(trunk_action)
    return False


async def _refresh_rebased_review_branches(
    *,
    bypass_readiness: bool,
    client: JjClient,
    github_client: GithubClient,
    resubmit_revisions: tuple[LandRevision, ...],
    remote_name: str,
    trunk_branch: str,
) -> tuple[tuple[LandAction, ...], LandAction | None]:
    if not resubmit_revisions:
        return (), None

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
    actions = tuple(
        LandAction(
            kind="review branch",
            body=t"refresh {ui.bookmark(revision.bookmark)} to "
            t"{revision.subject} "
            t"{ui.change_id(revision.change_id)}",
            status="applied",
        )
        for revision in resubmit_revisions
    )
    dismissed_action = await _check_post_resubmit_approvals(
        bypass_readiness=bypass_readiness,
        github_client=github_client,
        resubmit_revisions=resubmit_revisions,
        trunk_branch=trunk_branch,
    )
    return actions, dismissed_action


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


async def _finalize_planned_revisions(
    *,
    actions: list[LandAction],
    bookmark_cleanup_by_change_id: dict[str, ReviewBookmarkCleanupPlan],
    client: JjClient,
    execution_plan: LandPlan,
    github_client: GithubClient,
    journal: OperationJournal,
    merge_method: str | None,
    mutation_run: LandMutationRun,
    trunk_branch: str,
) -> tuple[str, ...]:
    """Finalize each landed PR bottom-up and return the finalized change IDs.

    A returned tuple shorter than the plan means the merge transport stopped
    fail-closed at a PR GitHub would not merge.
    """

    landed_head_change_id = (
        execution_plan.planned_revisions[-1].change_id
        if execution_plan.planned_revisions
        else None
    )
    finalized_change_ids: list[str] = []
    for landed_index, landed_revision in enumerate(execution_plan.planned_revisions):
        console.output(
            t"Finalizing PR #{landed_revision.pull_request_number} for "
            t"{landed_revision.subject} "
            t"{ui.change_id(landed_revision.change_id)}..."
        )
        journal.append(
            "planned_mutation",
            {
                "change_id": landed_revision.change_id,
                "mutation": "finalize_pull_request",
                "pull_request_number": landed_revision.pull_request_number,
            },
        )
        if execution_plan.via == "merge":
            if merge_method is None:
                raise AssertionError("The merge transport requires a resolved merge method.")
            final_pull_request, blocked_action = await _merge_landed_pull_request(
                cached_change=mutation_run.state_changes.get(landed_revision.change_id),
                github_client=github_client,
                landed_revision=landed_revision,
                merge_method=merge_method,
                trunk_branch=trunk_branch,
            )
            if blocked_action is not None or final_pull_request is None:
                if blocked_action is not None:
                    actions.append(blocked_action)
                return tuple(finalized_change_ids)
            applied_body = (
                t"merge PR #{landed_revision.pull_request_number} into "
                t"{ui.bookmark(trunk_branch)} on GitHub for "
                t"{landed_revision.subject} "
                t"{ui.change_id(landed_revision.change_id)}"
            )
        else:
            final_pull_request = await _finalize_landed_pull_request(
                cached_change=mutation_run.state_changes.get(landed_revision.change_id),
                github_client=github_client,
                landed_revision=landed_revision,
                trunk_branch=trunk_branch,
            )
            applied_body = (
                t"finalize PR #{landed_revision.pull_request_number} for "
                t"{landed_revision.subject} "
                t"{ui.change_id(landed_revision.change_id)}"
            )
        journal.append(
            "mutation_applied",
            {
                "change_id": landed_revision.change_id,
                "mutation": "finalize_pull_request",
                "pull_request": final_pull_request,
                "pull_request_number": landed_revision.pull_request_number,
            },
        )
        actions.append(
            LandAction(
                kind="pull request",
                body=applied_body,
                status="applied",
            )
        )
        finalized_change_ids.append(landed_revision.change_id)
        landed_parent_change_id = (
            execution_plan.planned_revisions[landed_index - 1].change_id
            if landed_index > 0
            else None
        )
        previous_change = mutation_run.state_changes.get(landed_revision.change_id)
        updated_change = _updated_landed_change(
            bookmark=landed_revision.bookmark,
            bookmark_managed=landed_revision.bookmark_managed,
            cached_change=previous_change,
            commit_id=landed_revision.commit_id,
            parent_change_id=landed_parent_change_id,
            pull_request=final_pull_request,
            stack_head_change_id=landed_head_change_id,
        )
        mutation_run.state_changes[landed_revision.change_id] = updated_change
        mutation_run.save_interim_state()
        journal.append(
            "saved_state_update",
            {
                "after": updated_change,
                "before": previous_change,
                "change_id": landed_revision.change_id,
            },
        )
        cleanup_plan = bookmark_cleanup_by_change_id.get(landed_revision.change_id)
        if cleanup_plan is not None:
            if cleanup_plan.can_forget:
                journal.append(
                    "planned_mutation",
                    {
                        "bookmark": cleanup_plan.bookmark,
                        "change_id": landed_revision.change_id,
                        "mutation": "cleanup_local_bookmark",
                    },
                )
            cleanup_actions = _apply_review_bookmark_cleanup(
                client, cleanup_plan, landed_revision
            )
            actions.extend(cleanup_actions)
            if cleanup_plan.can_forget:
                journal.append(
                    "mutation_applied",
                    {
                        "actions": tuple(action.message for action in cleanup_actions),
                        "bookmark": cleanup_plan.bookmark,
                        "change_id": landed_revision.change_id,
                        "mutation": "cleanup_local_bookmark",
                    },
                )
    return tuple(finalized_change_ids)


def _apply_review_bookmark_cleanup(
    client: JjClient,
    cleanup_plan: ReviewBookmarkCleanupPlan,
    landed_revision: LandRevision,
) -> tuple[LandAction, ...]:
    if not cleanup_plan.can_forget:
        return (cleanup_plan.action,)
    client.forget_bookmarks((cleanup_plan.bookmark,))
    return (
        LandAction(
            kind="local bookmark",
            body=t"forget {ui.bookmark(cleanup_plan.bookmark)} "
            t"for {ui.change_id(landed_revision.change_id)}",
            status="applied",
        ),
    )


def _retire_finalized_tracking(
    *,
    finalized_revisions: tuple[LandRevision, ...],
    journal: OperationJournal,
    mutation_run: LandMutationRun,
) -> tuple[LandAction, ...]:
    """Remove direct-push landed changes from active review tracking."""

    previous_changes = dict(mutation_run.state_changes)
    retired_revisions: list[LandRevision] = []
    for finalized_revision in finalized_revisions:
        if mutation_run.state_changes.pop(finalized_revision.change_id, None) is not None:
            retired_revisions.append(finalized_revision)
    if not retired_revisions:
        return ()

    mutation_run.save_interim_state()
    journal.record_saved_state_updates(
        before=previous_changes,
        after=mutation_run.state_changes,
    )
    return tuple(
        LandAction(
            kind="tracking",
            body=landed_tracking_retire_body(retired_revision),
            status="applied",
        )
        for retired_revision in retired_revisions
    )


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
                body=t"before pushing {ui.bookmark(trunk_branch)} because refreshing "
                t"{ui.bookmark(revision.bookmark)} dismissed the approval on "
                t"PR #{revision.pull_request_number}; request re-review and rerun "
                t"{ui.cmd('land')}",
                status="blocked",
            )
    return None


async def _finalize_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    landed_revision: LandRevision,
    trunk_branch: str,
) -> GithubPullRequest:
    try:
        pull_request = await github_client.get_pull_request(
            pull_number=landed_revision.pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not load PR #{landed_revision.pull_request_number} during land"
        ) from error
    pull_request = pull_request.normalize_state()
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                pull_number=pull_request.number,
                base=trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to "
                t"{ui.bookmark(trunk_branch)}"
            ) from error
        pull_request = pull_request.normalize_state()
    if pull_request.state == "open":
        try:
            await github_client.close_pull_request(
                pull_number=pull_request.number,
            )
            pull_request = await github_client.get_pull_request(
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            recovered_pull_request: GithubPullRequest | None = None
            if error.status_code == 422:
                try:
                    refreshed_pull_request = await github_client.get_pull_request(
                        pull_number=pull_request.number,
                    )
                except GithubClientError:
                    pass
                else:
                    refreshed_pull_request = refreshed_pull_request.normalize_state()
                    if refreshed_pull_request.state == "merged":
                        recovered_pull_request = refreshed_pull_request
            if recovered_pull_request is None:
                raise CliError(
                    t"Could not close PR #{pull_request.number} after landing"
                ) from error
            pull_request = recovered_pull_request
        pull_request = pull_request.normalize_state()
    await _delete_landed_stack_comments(
        cached_change=cached_change,
        github_client=github_client,
    )
    return pull_request


async def _merge_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    landed_revision: LandRevision,
    merge_method: str,
    trunk_branch: str,
) -> tuple[GithubPullRequest | None, LandAction | None]:
    """Retarget one landable PR to trunk and merge it through the GitHub API.

    Returns the merged pull request, or a blocking action when GitHub refuses
    the merge (pending checks, conflicts, or repo policy).
    """

    try:
        pull_request = await github_client.get_pull_request(
            pull_number=landed_revision.pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not load PR #{landed_revision.pull_request_number} during land"
        ) from error
    pull_request = pull_request.normalize_state()
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                pull_number=pull_request.number,
                base=trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to "
                t"{ui.bookmark(trunk_branch)}"
            ) from error
        pull_request = pull_request.normalize_state()
    if pull_request.state == "open":
        try:
            await github_client.merge_pull_request(
                pull_number=pull_request.number,
                merge_method=merge_method,
            )
        except GithubClientError as error:
            if error.status_code in (405, 409):
                return None, LandAction(
                    kind="boundary",
                    body=t"at PR #{pull_request.number} for {landed_revision.subject} "
                    t"{ui.change_id(landed_revision.change_id)}: GitHub reports it is "
                    t"not mergeable (pending checks, conflicts, or repo policy); make "
                    t"it mergeable and rerun {ui.cmd('land --via merge')}",
                    status="blocked",
                )
            raise CliError(
                t"Could not merge PR #{pull_request.number} on GitHub"
            ) from error
        try:
            pull_request = await github_client.get_pull_request(
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not reload PR #{pull_request.number} after merging"
            ) from error
        pull_request = pull_request.normalize_state()
    if pull_request.state != "merged":
        return None, LandAction(
            kind="boundary",
            body=t"at PR #{pull_request.number} for {landed_revision.subject} "
            t"{ui.change_id(landed_revision.change_id)}: the PR is "
            t"{pull_request.state} instead of merged; inspect it on GitHub and "
            t"rerun {ui.cmd('land --via merge')}",
            status="blocked",
        )
    await _delete_landed_stack_comments(
        cached_change=cached_change,
        github_client=github_client,
    )
    return pull_request, None


async def _delete_landed_stack_comments(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
) -> None:
    if cached_change is None:
        return
    comment_targets: tuple[tuple[int | None, StackCommentKind], ...] = (
        (cached_change.navigation_comment_id, "navigation"),
        (cached_change.overview_comment_id, "overview"),
    )
    for comment_id, kind in comment_targets:
        if comment_id is None:
            continue
        await delete_stack_comment(
            comment_id=comment_id,
            github_client=github_client,
            kind=kind,
        )


def _updated_landed_change(
    *,
    bookmark: str,
    bookmark_managed: bool,
    cached_change: CachedChange | None,
    commit_id: str,
    parent_change_id: str | None,
    pull_request: GithubPullRequest,
    stack_head_change_id: str | None,
) -> CachedChange:
    pr_state = pull_request.state
    if pull_request.merged_at is not None:
        pr_state = "merged"
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            bookmark_ownership="managed" if bookmark_managed else "external",
            last_submitted_commit_id=commit_id,
            last_submitted_parent_change_id=parent_change_id,
            last_submitted_stack_head_change_id=stack_head_change_id,
            pr_number=pull_request.number,
            pr_state=pr_state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": "managed" if bookmark_managed else "external",
            "last_submitted_commit_id": commit_id,
            "last_submitted_parent_change_id": parent_change_id,
            "last_submitted_stack_head_change_id": stack_head_change_id,
            "pr_number": pull_request.number,
            "pr_review_decision": None,
            "pr_state": pr_state,
            "pr_url": pull_request.html_url,
        }
    ).with_cleared_comments()
