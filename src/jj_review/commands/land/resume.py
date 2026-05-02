"""Interrupted land recovery helpers."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime

from jj_review import console, ui
from jj_review.errors import CliError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.models.intent import LandIntent, LoadedIntent
from jj_review.review.intents import (
    describe_intent,
    match_ordered_change_ids,
    retire_superseded_intents,
)
from jj_review.review.status import PreparedStatus
from jj_review.state.intents import check_same_kind_intent

from .models import (
    BookmarkStateReader,
    LandAction,
    LandExecutionState,
    LandPlan,
    LandResult,
    LandRevision,
    PreparedLand,
    ResumeLandIntent,
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

    current_landed_change_ids = tuple(revision.change_id for revision in plan.landed_revisions)
    stale_intents = check_same_kind_intent(
        state_dir,
        build_land_intent(
            bypass_readiness=prepared_land.bypass_readiness,
            cleanup_bookmarks=prepared_land.cleanup_bookmarks,
            landed_revisions=plan.landed_revisions,
            prepared_status=prepared_status,
            selected_pr_number=prepared_land.selected_pr_number,
            trunk_branch=trunk_branch,
        ),
    )
    resume_intent = _find_resume_land_intent(
        bypass_readiness=prepared_land.bypass_readiness,
        cleanup_bookmarks=prepared_land.cleanup_bookmarks,
        current_landed_change_ids=current_landed_change_ids,
        prepared_status=prepared_status,
        selected_pr_number=prepared_land.selected_pr_number,
        stale_intents=stale_intents,
        trunk_branch=trunk_branch,
    )
    report_stale_land_intents(
        current_landed_change_ids=current_landed_change_ids,
        prepared_status=prepared_status,
        resume_intent=resume_intent,
        stale_intents=stale_intents,
    )

    execution_plan = plan
    trunk_transition_already_succeeded = (
        resume_intent is not None
        and remote_trunk_matches_commit(
            client=prepared_status.prepared.client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            commit_id=resume_intent.intent.landed_commit_id,
        )
    )
    if trunk_transition_already_succeeded and resume_intent is not None:
        execution_plan = resume_land_plan(
            intent=resume_intent.intent,
            trunk_branch=trunk_branch,
        )

    if not execution_plan.landed_revisions and not execution_plan.push_trunk:
        if resume_intent is not None:
            retire_superseded_intents(stale_intents, resume_intent.intent)
            resume_intent.path.unlink(missing_ok=True)
        raise CompletedLandResume(
            LandResult(
                actions=(
                    LandAction(
                        kind="resume",
                        body="previous landing already completed; cleared stale intent",
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

    if not execution_plan.push_trunk and not execution_plan.landed_revisions:
        raise AssertionError("Resume execution without remaining work must be handled above.")
    return LandExecutionState(
        execution_plan=execution_plan,
        resume_intent=resume_intent,
        stale_intents=stale_intents,
        state_dir=state_dir,
    )


class CompletedLandResume(Exception):
    """Internal sentinel used when a resumed land already finished previously."""

    def __init__(self, result: LandResult) -> None:
        super().__init__("completed land resume")
        self.result = result


def report_stale_land_intents(
    *,
    current_landed_change_ids: tuple[str, ...],
    prepared_status: PreparedStatus,
    resume_intent: ResumeLandIntent | None,
    stale_intents: list[LoadedIntent],
) -> None:
    """Print resumable land intent diagnostics for live execution."""

    for loaded in stale_intents:
        if not isinstance(loaded.intent, LandIntent):
            continue
        if resume_intent is not None and loaded.path == resume_intent.path:
            if resume_intent.mode == "tail-after-landed-prefix":
                console.note(
                    t"Resuming interrupted {describe_intent(loaded.intent)} after the "
                    t"trunk transition already succeeded"
                )
            else:
                console.note(t"Resuming interrupted {describe_intent(loaded.intent)}")
            continue
        match = match_ordered_change_ids(
            loaded.intent.ordered_change_ids,
            tuple(
                prepared_revision.revision.change_id
                for prepared_revision in prepared_status.prepared.status_revisions
            ),
        )
        if match == "overlap":
            console.warning(
                t"this land overlaps an incomplete earlier operation "
                t"({describe_intent(loaded.intent)})"
            )
        else:
            console.note(t"incomplete operation outstanding: {describe_intent(loaded.intent)}")


def _find_resume_land_intent(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    current_landed_change_ids: tuple[str, ...],
    prepared_status: PreparedStatus,
    selected_pr_number: int | None,
    stale_intents: Sequence[LoadedIntent],
    trunk_branch: str,
) -> ResumeLandIntent | None:
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    tail_match: ResumeLandIntent | None = None
    for loaded in stale_intents:
        if not isinstance(loaded.intent, LandIntent):
            continue
        intent = loaded.intent
        if intent.display_revset != prepared_status.selected_revset:
            continue
        if intent.bypass_readiness != bypass_readiness:
            continue
        if intent.cleanup_bookmarks != cleanup_bookmarks:
            continue
        if intent.selected_pr_number != selected_pr_number or intent.trunk_branch != trunk_branch:
            continue
        if (
            intent.ordered_change_ids == current_change_ids
            and intent.ordered_commit_ids == current_commit_ids
            and intent.landed_change_ids == current_landed_change_ids
        ):
            return ResumeLandIntent(
                intent=intent,
                path=loaded.path,
                mode="exact-path",
            )
        prefix_length = len(intent.landed_change_ids)
        if intent.ordered_change_ids[:prefix_length] != intent.landed_change_ids:
            continue
        if (
            intent.ordered_change_ids[prefix_length:] == current_change_ids
            and intent.ordered_commit_ids[prefix_length:] == current_commit_ids
        ):
            tail_match = ResumeLandIntent(
                intent=intent,
                path=loaded.path,
                mode="tail-after-landed-prefix",
            )
    return tail_match


def remote_trunk_matches_commit(
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
    return remote_state is not None and remote_state.target == commit_id


def resume_land_plan(*, intent: LandIntent, trunk_branch: str) -> LandPlan:
    completed_change_ids = set(intent.completed_change_ids)
    landed_revisions: list[LandRevision] = []
    for change_id in intent.landed_change_ids:
        if change_id in completed_change_ids:
            continue
        try:
            landed_revisions.append(
                LandRevision(
                    bookmark=intent.landed_bookmarks[change_id],
                    bookmark_managed=intent.landed_bookmark_managed[change_id],
                    change_id=change_id,
                    commit_id=intent.landed_commit_ids[change_id],
                    needs_resubmit=False,
                    pull_request_number=intent.landed_pull_request_numbers[change_id],
                    subject=intent.landed_subjects[change_id],
                )
            )
        except KeyError as error:
            raise CliError(
                t"Interrupted land intent for {intent.label} is incomplete. "
                t"Re-run {ui.cmd('land')} to refresh the plan."
            ) from error
    return LandPlan(
        blocked=False,
        boundary_action=None,
        landed_revisions=tuple(landed_revisions),
        push_trunk=False,
        trunk_branch=trunk_branch,
    )


def build_land_intent(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    landed_revisions: tuple[LandRevision, ...],
    prepared_status: PreparedStatus,
    selected_pr_number: int | None,
    trunk_branch: str,
) -> LandIntent:
    ordered_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    ordered_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    landed_change_ids = tuple(revision.change_id for revision in landed_revisions)
    landed_commit_id = (
        landed_revisions[-1].commit_id
        if landed_revisions
        else prepared_status.prepared.stack.trunk.commit_id
    )
    return LandIntent(
        kind="land",
        pid=os.getpid(),
        label=f"land on {prepared_status.selected_revset}",
        bypass_readiness=bypass_readiness,
        cleanup_bookmarks=cleanup_bookmarks,
        display_revset=prepared_status.selected_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        landed_change_ids=landed_change_ids,
        landed_bookmarks={revision.change_id: revision.bookmark for revision in landed_revisions},
        landed_bookmark_managed={
            revision.change_id: revision.bookmark_managed for revision in landed_revisions
        },
        landed_commit_ids={
            revision.change_id: revision.commit_id for revision in landed_revisions
        },
        landed_pull_request_numbers={
            revision.change_id: revision.pull_request_number for revision in landed_revisions
        },
        landed_subjects={revision.change_id: revision.subject for revision in landed_revisions},
        completed_change_ids=(),
        trunk_branch=trunk_branch,
        landed_commit_id=landed_commit_id,
        selected_pr_number=selected_pr_number,
        started_at=datetime.now(UTC).isoformat(),
    )
