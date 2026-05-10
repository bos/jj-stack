"""Live execution for the land command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jj_review import console, ui
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.jj import JjClient
from jj_review.models.github import GithubPullRequest
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.state.journal import (
    LandOperationRecord,
    LoadedOperationRecord,
    OperationJournal,
    append_abandoned_event,
)
from jj_review.state.operation_lock import read_operation_lock_holder
from jj_review.state.store import ReviewStateStore

from .models import (
    BookmarkStateReader,
    LandAction,
    LandPlan,
    LandResult,
    LandRevision,
    PreparedLand,
    ReviewBookmarkCleanupPlan,
)
from .resume import CompletedLandResume, prepare_land_execution_state


@dataclass(frozen=True, slots=True)
class _LandResultContext:
    bypass_readiness: bool
    github_repository: str
    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str

    def result(
        self,
        *,
        actions: tuple[LandAction, ...],
        applied: bool,
        blocked: bool,
    ) -> LandResult:
        return LandResult(
            actions=actions,
            applied=applied,
            bypass_readiness=self.bypass_readiness,
            blocked=blocked,
            github_repository=self.github_repository,
            remote_name=self.remote_name,
            selected_revset=self.selected_revset,
            trunk_branch=self.trunk_branch,
            trunk_subject=self.trunk_subject,
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
    if remote_state is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available.",
            hint="Fetch and retry.",
        )
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is "
            t"conflicted.",
            hint="Resolve it before landing.",
        )
    if remote_state.target is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available.",
            hint="Fetch and retry.",
        )
    if remote_state.target != trunk_commit_id:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} moved since "
            t"the selected path was resolved.",
            hint="Fetch, rebase if needed, and retry.",
        )


async def execute_land_plan(
    *,
    bookmark_cleanup_plans: tuple[ReviewBookmarkCleanupPlan, ...],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
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
    result_context = _LandResultContext(
        bypass_readiness=prepared_land.bypass_readiness,
        github_repository=github_repository.full_name,
        remote_name=remote_name,
        selected_revset=selected_revset,
        trunk_branch=trunk_branch,
        trunk_subject=trunk_subject,
    )
    try:
        execution_state = prepare_land_execution_state(
            github_repository=github_repository,
            plan=plan,
            prepared_land=prepared_land,
            prepared_status=prepared_status,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
        )
    except CompletedLandResume as resume:
        return resume.result
    execution_plan = execution_state.execution_plan
    if execution_plan.blocked:
        return result_context.result(
            actions=execution_plan.planned_actions(),
            applied=False,
            blocked=True,
        )
    resume_operation = execution_state.resume_operation
    journal = (
        OperationJournal.open(resume_operation.path)
        if resume_operation is not None
        else OperationJournal.begin(
            execution_state.state_dir,
            operation="land",
            options={
                "bypass_readiness": prepared_land.bypass_readiness,
                "cleanup_bookmarks": prepared_land.cleanup_bookmarks,
                "selected_pr_number": prepared_land.selected_pr_number,
            },
            resolved_scope={
                "github_repository": github_repository.full_name,
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
                "planned_change_ids": tuple(
                    revision.change_id for revision in execution_plan.planned_revisions
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
            lock_holder=read_operation_lock_holder(execution_state.state_dir),
        )
    )
    if resume_operation is None and prepared_land.operation_lock is not None:
        prepared_land.operation_lock.record_journal_path(journal.path)

    state = prepared.state_store.load()
    state_changes = dict(state.changes)

    actions: list[LandAction] = []
    succeeded = False
    bookmark_cleanup_by_change_id = {
        cleanup_plan.change_id: cleanup_plan for cleanup_plan in bookmark_cleanup_plans
    }
    try:
        blocked_result = await _apply_trunk_transition(
            actions=actions,
            client=prepared.client,
            execution_plan=execution_plan,
            github_client=github_client,
            github_repository=github_repository,
            journal=journal,
            prepared_land=prepared_land,
            remote_name=remote_name,
            result_context=result_context,
            trunk_branch=trunk_branch,
        )
        if blocked_result is not None:
            return blocked_result
        await _finalize_planned_revisions(
            actions=actions,
            bookmark_cleanup_by_change_id=bookmark_cleanup_by_change_id,
            client=prepared.client,
            execution_plan=execution_plan,
            github_client=github_client,
            github_repository=github_repository,
            journal=journal,
            state=state,
            state_changes=state_changes,
            state_store=prepared.state_store,
            trunk_branch=trunk_branch,
        )
        journal.append(
            "completed",
            {
                "completed_change_ids": tuple(
                    revision.change_id for revision in execution_plan.planned_revisions
                )
            },
        )
        succeeded = True
        return result_context.result(
            actions=execution_plan.completed_actions(actions=tuple(actions)),
            applied=True,
            blocked=False,
        )
    finally:
        if succeeded:
            _retire_superseded_land_operations(
                execution_state.stale_operations,
                current_journal_path=journal.path,
                current_change_ids=tuple(
                    prepared_revision.revision.change_id
                    for prepared_revision in prepared_status.prepared.status_revisions
                ),
            )


async def _apply_trunk_transition(
    *,
    actions: list[LandAction],
    client: JjClient,
    execution_plan: LandPlan,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    journal: OperationJournal,
    prepared_land: PreparedLand,
    remote_name: str,
    result_context: _LandResultContext,
    trunk_branch: str,
) -> LandResult | None:
    if not execution_plan.push_trunk:
        return None

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
    refresh_actions, dismissed_action = await _refresh_rebased_review_branches(
        bypass_readiness=prepared_land.bypass_readiness,
        client=client,
        github_client=github_client,
        github_repository=github_repository,
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
        return result_context.result(
            actions=tuple(actions),
            applied=True,
            blocked=True,
        )

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
    )
    actions.append(trunk_action)
    return None


async def _refresh_rebased_review_branches(
    *,
    bypass_readiness: bool,
    client: JjClient,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
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
        github_repository=github_repository,
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
    github_repository: ParsedGithubRepo,
    journal: OperationJournal,
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    trunk_branch: str,
) -> None:
    landed_head_change_id = (
        execution_plan.planned_revisions[-1].change_id
        if execution_plan.planned_revisions
        else None
    )
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
        final_pull_request = await _finalize_landed_pull_request(
            cached_change=state_changes.get(landed_revision.change_id),
            github_client=github_client,
            github_repository=github_repository,
            landed_revision=landed_revision,
            trunk_branch=trunk_branch,
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
                body=t"finalize PR #{landed_revision.pull_request_number} for "
                t"{landed_revision.subject} "
                t"{ui.change_id(landed_revision.change_id)}",
                status="applied",
            )
        )
        landed_parent_change_id = (
            execution_plan.planned_revisions[landed_index - 1].change_id
            if landed_index > 0
            else None
        )
        previous_change = state_changes.get(landed_revision.change_id)
        updated_change = _updated_landed_change(
            bookmark=landed_revision.bookmark,
            bookmark_managed=landed_revision.bookmark_managed,
            cached_change=previous_change,
            commit_id=landed_revision.commit_id,
            parent_change_id=landed_parent_change_id,
            pull_request=final_pull_request,
            stack_head_change_id=landed_head_change_id,
        )
        state_changes[landed_revision.change_id] = updated_change
        state_store.save(state.model_copy(update={"changes": dict(state_changes)}))
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
    return None


def _retire_superseded_land_operations(
    stale_operations: list[LoadedOperationRecord],
    *,
    current_journal_path: Path,
    current_change_ids: tuple[str, ...],
) -> None:
    for loaded in stale_operations:
        if loaded.path == current_journal_path:
            continue
        if not isinstance(loaded.operation, LandOperationRecord):
            continue
        if match_ordered_land_operation(loaded.operation.ordered_change_ids, current_change_ids):
            append_abandoned_event(
                loaded.path,
                reason="superseded_by_successful_land",
            )


def match_ordered_land_operation(
    existing: tuple[str, ...],
    new: tuple[str, ...],
) -> bool:
    if existing == new:
        return True
    return len(new) > len(existing) and new[: len(existing)] == existing


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


async def _check_post_resubmit_approvals(
    *,
    bypass_readiness: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    resubmit_revisions: tuple[LandRevision, ...],
    trunk_branch: str,
) -> LandAction | None:
    """Return a blocking action if the resubmit push dismissed any approval."""

    if bypass_readiness or not resubmit_revisions:
        return None
    try:
        decisions = await github_client.get_review_decisions_by_pull_request_numbers(
            github_repository.owner,
            github_repository.repo,
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
    github_repository: ParsedGithubRepo,
    landed_revision: LandRevision,
    trunk_branch: str,
) -> GithubPullRequest:
    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
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
                github_repository.owner,
                github_repository.repo,
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
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
            pull_request = await github_client.get_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            raise CliError(t"Could not close PR #{pull_request.number} after landing") from error
        pull_request = pull_request.normalize_state()
    if cached_change is not None:
        for comment_id, label in (
            (cached_change.navigation_comment_id, "stack navigation comment"),
            (cached_change.overview_comment_id, "stack overview comment"),
        ):
            if comment_id is None:
                continue
            try:
                await github_client.delete_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=comment_id,
                )
            except GithubClientError as error:
                if error.status_code != 404:
                    raise CliError(t"Could not delete {label} #{comment_id}") from error
    return pull_request


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
            "navigation_comment_id": None,
            "overview_comment_id": None,
        }
    )
