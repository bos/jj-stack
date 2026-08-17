"""Planning helpers for the merge command."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import short_change_id
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.ui import Message

from .models import MergeAction, MergeChange, MergePlan
from .preconditions import explain_precondition, merge_precondition_error


def build_merge_plan(
    *,
    observation: RepoFacts,
    remote_name: str,
    repo: GithubRepoAddress,
    changes: tuple[LocalCommit, ...],
    state: TrackingState,
    target_change_id: str | None,
    trunk_branch: str,
) -> MergePlan:
    merge_changes = tuple(_merge_change(observation, change, state) for change in changes)
    candidates: list[MergeChange] = []
    boundary: Message | None = None
    for local, change in zip(changes, merge_changes, strict=True):
        if change is None:
            boundary = _boundary(local, t"run {ui.cmd('relink')} before merging")
            break
        error = merge_precondition_error(
            expected_repo=repo,
            expected_trunk_branch=trunk_branch,
            observation=observation,
            remote_name=remote_name,
            changes=(change,),
        )
        if error is not None:
            boundary = _boundary(
                local,
                explain_precondition(
                    error,
                    change_id=change.change_id,
                    sync_target=short_change_id(changes[-1].change_id),
                ),
            )
            break
        candidates.append(change)
    if target_change_id is not None:
        target = next(
            (
                index + 1
                for index, change in enumerate(candidates)
                if change.change_id == target_change_id
            ),
            0,
        )
        candidates = candidates[:target]
        boundary = (
            None
            if target
            else boundary
            or (
                "the selected PR is above the PRs that can merge right now; run "
                "jj-stack view to see which PRs at the bottom of the stack are ready"
            )
        )
    action = (
        MergeAction(
            kind="boundary",
            body=boundary or "No PRs on the selected stack can be merged.",
            status="blocked" if not candidates else "planned",
        )
        if boundary is not None or not candidates
        else None
    )
    return MergePlan(
        boundary_action=action,
        planned_changes=tuple(candidates),
        linked_changes=tuple(change for change in merge_changes if change is not None),
    )


def _merge_change(
    observation: RepoFacts,
    change: LocalCommit,
    state: TrackingState,
) -> MergeChange | None:
    candidate = state.tracked_pr(change.change_id)
    pr = observation.prs[change.change_id].pr
    if candidate is None or pr is None:
        return None
    return MergeChange(
        base_ref=pr.base.ref,
        change_id=change.change_id,
        commit_id=change.commit_id,
        identity=candidate.pr_identity,
        subject=change.subject,
    )


def _boundary(change: LocalCommit, reason: Message) -> Message:
    return (
        t"before {change.subject} {ui.change_id(change.change_id)} because ",
        reason,
    )
