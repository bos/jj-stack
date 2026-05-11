"""Interrupted land recovery helpers."""

from __future__ import annotations

from collections.abc import Sequence

from jj_review import console, ui
from jj_review.errors import CliError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.review.change_status import classify_review_change
from jj_review.review.operations import (
    describe_operation,
    match_ordered_change_ids,
)
from jj_review.review.status import PreparedStatus
from jj_review.state.journal import (
    LandOperationRecord,
    LoadedOperationRecord,
    OperationJournal,
    read_journal,
)

from .models import (
    BookmarkStateReader,
    LandAction,
    LandExecutionState,
    LandPlan,
    LandResult,
    LandRevision,
    PreparedLand,
    ResumeLandOperation,
)


def prepare_land_execution_state(
    *,
    github_repository: ParsedGithubRepo,
    plan: LandPlan,
    prepared_land: PreparedLand,
    prepared_status: PreparedStatus,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_subject: str,
) -> LandExecutionState:
    """Resolve resume state before live execution."""

    state_dir = prepared_status.prepared.state_store.require_writable()

    current_planned_change_ids = tuple(revision.change_id for revision in plan.planned_revisions)
    stale_operations = [
        loaded
        for loaded in prepared_status.prepared.state_store.list_operations()
        if isinstance(loaded.operation, LandOperationRecord)
    ]
    resume_operation = _find_resume_land_operation(
        bypass_readiness=prepared_land.bypass_readiness,
        cleanup_bookmarks=prepared_land.cleanup_bookmarks,
        current_planned_change_ids=current_planned_change_ids,
        prepared_status=prepared_status,
        selected_pr_number=prepared_land.selected_pr_number,
        stale_operations=stale_operations,
        trunk_branch=trunk_branch,
    )
    _report_stale_land_operations(
        prepared_status=prepared_status,
        resume_operation=resume_operation,
        stale_operations=stale_operations,
    )

    execution_plan = plan
    trunk_transition_already_succeeded = (
        resume_operation is not None
        and _remote_trunk_matches_commit(
            client=prepared_status.prepared.client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            commit_id=resume_operation.operation.landed_commit_id,
        )
    )
    if trunk_transition_already_succeeded and resume_operation is not None:
        execution_plan = _resume_land_plan(
            operation=resume_operation.operation,
            trunk_branch=trunk_branch,
        )

    if (
        resume_operation is not None
        and not execution_plan.planned_revisions
        and not execution_plan.push_trunk
    ):
        OperationJournal.open(resume_operation.path).append(
            "completed",
            {"completed_change_ids": resume_operation.operation.landed_change_ids},
        )
        raise CompletedLandResume(
            LandResult(
                actions=(
                    LandAction(
                        kind="resume",
                        body="previous landing already completed; cleared operation record",
                        status="applied",
                    ),
                ),
                applied=True,
                bypass_readiness=prepared_land.bypass_readiness,
                blocked=False,
                github_repository=github_repository.full_name,
                remote_name=remote_name,
                selected_revset=selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=trunk_subject,
            )
        )

    if not execution_plan.planned_revisions and not execution_plan.push_trunk:
        if execution_plan.blocked:
            return LandExecutionState(
                execution_plan=execution_plan,
                resume_operation=resume_operation,
                stale_operations=stale_operations,
                state_dir=state_dir,
            )
        raise AssertionError("Resume execution without remaining work must be handled above.")

    return LandExecutionState(
        execution_plan=execution_plan,
        resume_operation=resume_operation,
        stale_operations=stale_operations,
        state_dir=state_dir,
    )


class CompletedLandResume(Exception):
    """Internal sentinel used when a resumed land already finished previously."""

    def __init__(self, result: LandResult) -> None:
        super().__init__("completed land resume")
        self.result = result


def _report_stale_land_operations(
    *,
    prepared_status: PreparedStatus,
    resume_operation: ResumeLandOperation | None,
    stale_operations: list[LoadedOperationRecord],
) -> None:
    """Print resumable land operation diagnostics for live execution."""

    for loaded in stale_operations:
        if not isinstance(loaded.operation, LandOperationRecord):
            continue
        if resume_operation is not None and loaded.path == resume_operation.path:
            if resume_operation.mode == "tail-after-landed-prefix":
                console.note(
                    t"Resuming interrupted {describe_operation(loaded.operation)} after the "
                    t"trunk transition already succeeded"
                )
            else:
                console.note(t"Resuming interrupted {describe_operation(loaded.operation)}")
            continue
        match = match_ordered_change_ids(
            loaded.operation.ordered_change_ids,
            tuple(
                prepared_revision.revision.change_id
                for prepared_revision in prepared_status.prepared.status_revisions
            ),
        )
        if match == "overlap":
            console.warning(
                t"this land overlaps an incomplete earlier operation "
                t"({describe_operation(loaded.operation)})"
            )
        else:
            console.note(
                t"incomplete operation outstanding: {describe_operation(loaded.operation)}"
            )


def _find_resume_land_operation(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    current_planned_change_ids: tuple[str, ...],
    prepared_status: PreparedStatus,
    selected_pr_number: int | None,
    stale_operations: Sequence[LoadedOperationRecord],
    trunk_branch: str,
) -> ResumeLandOperation | None:
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    tail_match: ResumeLandOperation | None = None
    for loaded in stale_operations:
        operation = loaded.operation
        if not isinstance(operation, LandOperationRecord):
            continue
        if operation.display_revset != prepared_status.selected_revset:
            continue
        if operation.bypass_readiness != bypass_readiness:
            continue
        if operation.cleanup_bookmarks != cleanup_bookmarks:
            continue
        if (
            operation.selected_pr_number != selected_pr_number
            or operation.trunk_branch != trunk_branch
        ):
            continue
        if (
            operation.ordered_change_ids == current_change_ids
            and operation.ordered_commit_ids == current_commit_ids
            and operation.landed_change_ids == current_planned_change_ids
        ):
            return ResumeLandOperation(
                operation=operation,
                path=loaded.path,
                mode="exact-path",
            )
        prefix_length = len(operation.landed_change_ids)
        if operation.ordered_change_ids[:prefix_length] != operation.landed_change_ids:
            continue
        if (
            operation.ordered_change_ids[prefix_length:] == current_change_ids
            and operation.ordered_commit_ids[prefix_length:] == current_commit_ids
        ):
            tail_match = ResumeLandOperation(
                operation=operation,
                path=loaded.path,
                mode="tail-after-landed-prefix",
            )
    return tail_match


def _remote_trunk_matches_commit(
    *,
    client: BookmarkStateReader,
    remote_name: str,
    trunk_branch: str,
    commit_id: str,
) -> bool:
    bookmark_state = client.get_bookmark_state(trunk_branch)
    local_target = bookmark_state.local_target
    if local_target is not None and local_target != commit_id:
        return False
    remote_state = bookmark_state.remote_target(remote_name)
    review_status = classify_review_change(
        cached_change=None,
        commit_id=commit_id,
        local="present",
        pull_request_lookup=None,
        remote_state=remote_state,
    )
    return review_status.remote_branch_matches_commit is True


def _resume_land_plan(*, operation: LandOperationRecord, trunk_branch: str) -> LandPlan:
    completed_change_ids = set(_completed_land_change_ids(operation))
    planned_revisions: list[LandRevision] = []
    for change_id in operation.landed_change_ids:
        if change_id in completed_change_ids:
            continue
        try:
            planned_revisions.append(
                LandRevision(
                    bookmark=operation.landed_bookmarks[change_id],
                    bookmark_managed=operation.landed_bookmark_managed[change_id],
                    change_id=change_id,
                    commit_id=operation.landed_commit_ids[change_id],
                    needs_resubmit=False,
                    pull_request_number=operation.landed_pull_request_numbers[change_id],
                    subject=operation.landed_subjects[change_id],
                )
            )
        except KeyError as error:
            raise CliError(
                t"Interrupted land journal for {describe_operation(operation)} is incomplete. "
                t"Re-run {ui.cmd('land')} to refresh the plan."
            ) from error
    return LandPlan(
        blocked=False,
        boundary_action=None,
        planned_revisions=tuple(planned_revisions),
        push_trunk=False,
        trunk_branch=trunk_branch,
    )


def _completed_land_change_ids(operation: LandOperationRecord) -> tuple[str, ...]:
    """Return the landed prefix whose saved-state updates are durably recorded."""

    try:
        events = read_journal(operation.path)
    except (OSError, ValueError, KeyError) as error:
        raise CliError(
            t"Interrupted land journal for {describe_operation(operation)} is unreadable. "
            t"Re-run {ui.cmd('land')} to refresh the plan."
        ) from error
    completed: list[str] = []
    for event in events:
        if event.event != "saved_state_update":
            continue
        change_id = event.data.get("change_id")
        if isinstance(change_id, str):
            completed.append(change_id)
    return tuple(dict.fromkeys(completed))
