"""Policy helpers for interrupted submit recovery."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jj_review.models.bookmarks import BookmarkState
from jj_review.models.review_state import CachedChange
from jj_review.state.journal import SubmitOperationRecord


class SubmitStackRelation(StrEnum):
    EXACT = "exact"
    REWRITTEN = "rewritten"
    DISJOINT = "disjoint"


class SubmitTargetRelation(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class SubmitStatusDecision(StrEnum):
    CONTINUE = "continue"
    CURRENT_STACK = "current-stack"
    INSPECT = "inspect"
    OUTSTANDING = "outstanding"


class ArtifactPresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SubmitRecoveryIdentity:
    remote_name: str
    github_host: str
    github_owner: str
    github_repo: str

    @classmethod
    def from_operation(cls, operation: SubmitOperationRecord) -> SubmitRecoveryIdentity:
        return cls(
            remote_name=operation.remote_name,
            github_host=operation.github_host,
            github_owner=operation.github_owner,
            github_repo=operation.github_repo,
        )

    @classmethod
    def from_github_repository(
        cls,
        *,
        remote_name: str,
        github_repository,
    ) -> SubmitRecoveryIdentity:
        return cls(
            remote_name=remote_name,
            github_host=github_repository.host,
            github_owner=github_repository.owner,
            github_repo=github_repository.repo,
        )


@dataclass(frozen=True)
class SubmitArtifactObservation:
    target_relation: SubmitTargetRelation
    saved_state: ArtifactPresence
    local_bookmarks: ArtifactPresence
    remote_bookmarks: ArtifactPresence


def submit_stack_relation(
    *,
    intent: SubmitOperationRecord,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> SubmitStackRelation:
    """Classify how the current stack relates to a recorded submit snapshot."""

    if intent.ordered_commit_ids and intent.ordered_commit_ids == current_commit_ids:
        return SubmitStackRelation.EXACT
    if set(intent.ordered_change_ids) & set(current_change_ids):
        return SubmitStackRelation.REWRITTEN
    return SubmitStackRelation.DISJOINT


def submit_target_relation(
    *,
    intent: SubmitOperationRecord,
    current_identity: SubmitRecoveryIdentity | None,
) -> SubmitTargetRelation:
    """Classify whether the current submit target matches the recorded one."""

    if current_identity is None:
        return SubmitTargetRelation.UNKNOWN
    if SubmitRecoveryIdentity.from_operation(intent) == current_identity:
        return SubmitTargetRelation.MATCH
    return SubmitTargetRelation.MISMATCH


def submit_status_decision(
    *,
    intent: SubmitOperationRecord,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
    current_identity: SubmitRecoveryIdentity | None,
) -> SubmitStatusDecision:
    """Return the user-facing recovery decision for an interrupted submit."""

    stack_relation = submit_stack_relation(
        intent=intent,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )
    if stack_relation is SubmitStackRelation.EXACT:
        target_relation = submit_target_relation(
            intent=intent,
            current_identity=current_identity,
        )
        if target_relation is SubmitTargetRelation.MATCH:
            return SubmitStatusDecision.CONTINUE
        return SubmitStatusDecision.INSPECT
    if stack_relation is SubmitStackRelation.REWRITTEN:
        return SubmitStatusDecision.CURRENT_STACK
    return SubmitStatusDecision.OUTSTANDING


def recorded_submit_still_exists_exactly(
    *,
    intent: SubmitOperationRecord,
    commit_ids_by_change_id: dict[str, str],
) -> bool:
    """Return whether a recorded submit stack still exists exactly in the repo."""

    if not intent.ordered_commit_ids:
        return False
    if len(intent.ordered_commit_ids) != len(intent.ordered_change_ids):
        return False
    current_commit_ids = []
    for change_id in intent.ordered_change_ids:
        commit_id = commit_ids_by_change_id.get(change_id)
        if commit_id is None:
            return False
        current_commit_ids.append(commit_id)
    return tuple(current_commit_ids) == intent.ordered_commit_ids


def should_retire_submit_after_submit(
    *,
    old_operation: SubmitOperationRecord,
    new_operation: SubmitOperationRecord,
) -> bool:
    """Return whether a later successful submit clearly supersedes an older one."""

    if SubmitRecoveryIdentity.from_operation(
        old_operation
    ) != SubmitRecoveryIdentity.from_operation(new_operation):
        return False
    return bool(old_operation.bookmarks) and set(old_operation.bookmarks.values()).issubset(
        new_operation.bookmarks.values()
    )


def observe_submit_artifacts(
    *,
    current_changes: dict[str, CachedChange],
    intent: SubmitOperationRecord,
    bookmark_states: dict[str, BookmarkState],
    target_relation: SubmitTargetRelation,
) -> SubmitArtifactObservation:
    """Summarize whether a recorded submit still has live review artifacts."""

    intent_bookmarks = frozenset(intent.bookmarks.values())
    saved_state_live = any(
        _cached_change_has_live_review_artifacts(cached_change)
        for change_id in intent.ordered_change_ids
        if (cached_change := current_changes.get(change_id)) is not None
    ) or any(
        cached_change.bookmark in intent_bookmarks
        and _cached_change_has_live_review_artifacts(cached_change)
        for cached_change in current_changes.values()
    )
    local_bookmark_live = any(
        bookmark_states[bookmark].local_target is not None for bookmark in intent_bookmarks
    )

    return SubmitArtifactObservation(
        target_relation=target_relation,
        saved_state=(ArtifactPresence.PRESENT if saved_state_live else ArtifactPresence.ABSENT),
        local_bookmarks=(
            ArtifactPresence.PRESENT if local_bookmark_live else ArtifactPresence.ABSENT
        ),
        remote_bookmarks=_observe_remote_submit_bookmarks(
            bookmark_states=bookmark_states,
            intent=intent,
            intent_bookmarks=intent_bookmarks,
            target_relation=target_relation,
        ),
    )


def submit_artifacts_still_live(observation: SubmitArtifactObservation) -> bool:
    """Return whether recorded submit artifacts may still require recovery."""

    if observation.target_relation is not SubmitTargetRelation.MATCH:
        return True
    return any(
        presence is not ArtifactPresence.ABSENT
        for presence in (
            observation.saved_state,
            observation.local_bookmarks,
            observation.remote_bookmarks,
        )
    )


def should_retire_submit_after_cleanup(
    *,
    observation: SubmitArtifactObservation,
) -> bool:
    """Return whether cleanup has proven the recorded submit artifacts are gone."""

    return not submit_artifacts_still_live(observation)


def _cached_change_has_live_review_artifacts(cached_change: CachedChange) -> bool:
    """Return whether saved state still points at actionable review artifacts."""

    if (
        cached_change.navigation_comment_id is not None
        or cached_change.overview_comment_id is not None
    ):
        return True
    if cached_change.pr_state in {"closed", "merged"}:
        return False
    if cached_change.pr_review_decision is not None:
        return True
    return any(
        value is not None
        for value in (
            cached_change.pr_number,
            cached_change.pr_state,
            cached_change.pr_url,
        )
    )


def _observe_remote_submit_bookmarks(
    *,
    bookmark_states: dict[str, BookmarkState],
    intent: SubmitOperationRecord,
    intent_bookmarks: frozenset[str],
    target_relation: SubmitTargetRelation,
) -> ArtifactPresence:
    """Return whether the recorded remote bookmarks can still be observed."""

    if target_relation is not SubmitTargetRelation.MATCH:
        return ArtifactPresence.UNKNOWN

    for bookmark in intent_bookmarks:
        remote_state = bookmark_states[bookmark].remote_target(intent.remote_name)
        if remote_state is not None and remote_state.targets:
            return ArtifactPresence.PRESENT
    return ArtifactPresence.ABSENT
