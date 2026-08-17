"""Staleness detection for tracked stack changes."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.bootstrap import CommandContext
from jj_stack.stack.repo import observe_repo_paths


@dataclass(frozen=True, slots=True)
class LocalCleanupObservation:
    """Why one tracked change is stale locally, if it is."""

    has_mutable_copy: bool
    stale_reason: str | None


def local_cleanup_observations(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
) -> dict[str, LocalCleanupObservation]:
    jj_client = context.jj_client
    matched_commits = jj_client.query_commits_by_change_ids(change_ids)
    observations: dict[str, LocalCleanupObservation] = {}

    for change_id in change_ids:
        commits = matched_commits.get(change_id, ())
        if not commits:
            observations[change_id] = LocalCleanupObservation(
                has_mutable_copy=False,
                stale_reason="no visible local copy remains",
            )
            continue
        if len(commits) > 1:
            observations[change_id] = LocalCleanupObservation(
                has_mutable_copy=any(not commit.immutable for commit in commits),
                stale_reason="multiple visible commits still share that change ID",
            )
            continue

        change = commits[0]
        if not change.is_submittable():
            observations[change_id] = LocalCleanupObservation(
                has_mutable_copy=not change.immutable,
                stale_reason="local change is no longer an active stack member",
            )
            continue

        observations[change_id] = LocalCleanupObservation(
            has_mutable_copy=True,
            stale_reason=None,
        )

    candidate_changes = tuple(
        commits[0]
        for change_id in change_ids
        if observations[change_id].stale_reason is None
        for commits in (matched_commits.get(change_id, ()),)
        if commits
    )
    if not candidate_changes:
        return observations
    repo_paths = observe_repo_paths(
        jj_client=jj_client,
        descendant_of=tuple(change.commit_id for change in candidate_changes),
        state=context.state_store.load(),
    )
    supported_commit_ids = {
        change.commit_id for path in repo_paths.paths for change in path.stack.changes
    }
    for change in candidate_changes:
        if change.commit_id not in supported_commit_ids:
            observations[change.change_id] = LocalCleanupObservation(
                has_mutable_copy=True,
                stale_reason="local change is no longer part of a stack",
            )
    return observations
