"""Classify direct remote PR-branch updates for submit."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.errors import DriftError
from jj_stack.models.git import GitRemote
from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.pr_branches import ResolvedPRBranch

from .models import PreparedSubmitChange


def prepare_submit_changes(
    *,
    branch_resolutions: tuple[ResolvedPRBranch, ...],
    remote_targets: dict[str, str],
    remote: GitRemote,
    stack: LocalStack,
    state: TrackingState,
) -> tuple[PreparedSubmitChange, ...]:
    """Validate saved leases and describe the one atomic remote update."""

    prepared: list[PreparedSubmitChange] = []
    for resolution, change in zip(branch_resolutions, stack.changes, strict=True):
        tracked_pr = state.tracked_pr(change.change_id)
        remote_target = remote_targets.get(resolution.branch)

        if tracked_pr is not None:
            identity = tracked_pr.pr_identity
            if identity.head_ref != resolution.branch:
                raise DriftError(
                    t"Saved PR tracking for {ui.change_id(change.change_id)} names branch "
                    t"{ui.bookmark(identity.head_ref)}, not "
                    t"{ui.bookmark(resolution.branch)}.",
                    condition="saved_pr_mismatch",
                    hint=t"Run {ui.cmd('relink')} before submitting again.",
                )
            if remote_target is None:
                raise DriftError(
                    t"PR branch "
                    t"{ui.bookmark(f'{resolution.branch}@{remote.name}')} no longer exists.",
                    condition="remote_branch_missing",
                    hint=(
                        t"Restore the branch, or close the PR on GitHub, run "
                        t"{ui.cmd('jj-stack cleanup')}, and submit it again."
                    ),
                )
            if remote_target not in {
                tracked_pr.submitted_baseline.commit_id,
                change.commit_id,
            }:
                raise DriftError(
                    t"PR branch "
                    t"{ui.bookmark(f'{resolution.branch}@{remote.name}')} points to an "
                    t"unexpected commit.",
                    condition="remote_branch_moved",
                    hint=(
                        t"Inspect it with {ui.cmd('view')} and repair the PR "
                        t"before submitting again."
                    ),
                )
        elif resolution.recovered_target is not None:
            if remote_target != resolution.recovered_target:
                raise DriftError(
                    t"Recovered remote branch "
                    t"{ui.bookmark(f'{resolution.branch}@{remote.name}')} changed during "
                    t"submission.",
                    condition="remote_branch_moved",
                    hint="Inspect the branch and retry.",
                )
        elif remote_target not in {None, change.commit_id}:
            raise DriftError(
                t"PR branch "
                t"{ui.bookmark(f'{resolution.branch}@{remote.name}')} already exists and "
                t"points to another change.",
                condition="remote_branch_moved",
                hint="Move or delete the conflicting branch, then retry.",
            )

        prepared.append(
            PreparedSubmitChange(
                branch=resolution.branch,
                expected_remote_target=remote_target,
                remote_action=("up to date" if remote_target == change.commit_id else "pushed"),
                change=change,
            )
        )
    return tuple(prepared)
