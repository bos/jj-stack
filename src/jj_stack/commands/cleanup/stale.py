"""Staleness detection for tracked review changes."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.bootstrap import CommandContext
from jj_stack.models.stack import LocalRevision
from jj_stack.review.discovery import discover_stacks_from_revisions


@dataclass(frozen=True, slots=True)
class LocalCleanupObservation:
    """Why one tracked change is stale locally, if it is."""

    stale_reason: str | None


def _local_cleanup_observations(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
) -> dict[str, LocalCleanupObservation]:
    jj_client = context.jj_client
    matched_revisions = jj_client.query_revisions_by_change_ids(change_ids)
    observations: dict[str, LocalCleanupObservation] = {}

    for change_id in change_ids:
        revisions = matched_revisions.get(change_id, ())
        if not revisions:
            observations[change_id] = LocalCleanupObservation(
                stale_reason="no visible local change matches that cached change ID",
            )
            continue
        if len(revisions) > 1:
            observations[change_id] = LocalCleanupObservation(
                stale_reason="multiple visible revisions still share that change ID",
            )
            continue

        revision = revisions[0]
        if not revision.is_reviewable():
            observations[change_id] = LocalCleanupObservation(
                stale_reason="local change is no longer reviewable",
            )
            continue

        observations[change_id] = LocalCleanupObservation(
            stale_reason=None,
        )

    candidate_revisions = tuple(
        revisions[0]
        for change_id in change_ids
        if observations[change_id].stale_reason is None
        for revisions in (matched_revisions.get(change_id, ()),)
        if revisions
    )
    supported_commit_ids = _supported_review_commit_ids_for_revisions(
        context=context,
        revisions=candidate_revisions,
    )
    for revision in candidate_revisions:
        if revision.commit_id not in supported_commit_ids:
            observations[revision.change_id] = LocalCleanupObservation(
                stale_reason="local change no longer participates in a supported stack",
            )
    return observations


def _supported_review_commit_ids_for_revisions(
    *,
    context: CommandContext,
    revisions: tuple[LocalRevision, ...],
) -> set[str]:
    stacks = discover_stacks_from_revisions(
        jj_client=context.jj_client,
        revisions=revisions,
    )
    return {revision.commit_id for stack in stacks for revision in stack.revisions}
