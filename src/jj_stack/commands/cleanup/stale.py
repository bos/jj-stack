"""Staleness detection for tracked review changes."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.bootstrap import CommandContext
from jj_stack.review.repository import observe_repository_paths


@dataclass(frozen=True, slots=True)
class LocalCleanupObservation:
    """Why one tracked change is stale locally, if it is."""

    has_mutable_copy: bool
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
                has_mutable_copy=False,
                stale_reason="no visible local copy remains",
            )
            continue
        if len(revisions) > 1:
            observations[change_id] = LocalCleanupObservation(
                has_mutable_copy=any(not revision.immutable for revision in revisions),
                stale_reason="multiple visible revisions still share that change ID",
            )
            continue

        revision = revisions[0]
        if not revision.is_reviewable():
            observations[change_id] = LocalCleanupObservation(
                has_mutable_copy=not revision.immutable,
                stale_reason="local change is no longer reviewable",
            )
            continue

        observations[change_id] = LocalCleanupObservation(
            has_mutable_copy=True,
            stale_reason=None,
        )

    candidate_revisions = tuple(
        revisions[0]
        for change_id in change_ids
        if observations[change_id].stale_reason is None
        for revisions in (matched_revisions.get(change_id, ()),)
        if revisions
    )
    if not candidate_revisions:
        return observations
    repository_paths = observe_repository_paths(
        jj_client=jj_client,
        descendant_of=tuple(revision.commit_id for revision in candidate_revisions),
        state=context.state_store.load(),
    )
    supported_commit_ids = {
        revision.commit_id for path in repository_paths.paths for revision in path.stack.revisions
    }
    for revision in candidate_revisions:
        if revision.commit_id not in supported_commit_ids:
            observations[revision.change_id] = LocalCleanupObservation(
                has_mutable_copy=True,
                stale_reason="local change is no longer part of a review stack",
            )
    return observations
