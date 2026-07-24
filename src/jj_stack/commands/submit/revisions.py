"""Classify direct remote review-branch updates for submit."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.errors import CliError, DriftError
from jj_stack.models.git import GitRemote
from jj_stack.models.review_state import ReviewState
from jj_stack.models.stack import LocalStack
from jj_stack.review.branches import ResolvedReviewBranch

from .models import PreparedSubmitRevision


def prepare_submit_revisions(
    *,
    branch_resolutions: tuple[ResolvedReviewBranch, ...],
    remote_targets: dict[str, str],
    remote: GitRemote,
    stack: LocalStack,
    state: ReviewState,
) -> tuple[PreparedSubmitRevision, ...]:
    """Validate saved leases and describe the one atomic remote update."""

    prepared: list[PreparedSubmitRevision] = []
    for resolution, revision in zip(branch_resolutions, stack.revisions, strict=True):
        identity = state.review_identities.get(revision.change_id)
        baseline = state.submitted_baselines.get(revision.change_id)
        remote_target = remote_targets.get(resolution.branch)

        if identity is not None:
            if baseline is None:
                raise CliError(
                    t"Saved PR tracking for {ui.change_id(revision.change_id)} has no last "
                    t"submitted commit.",
                    hint=t"Repair it with {ui.cmd('relink')} before submitting again.",
                )
            if identity.head_ref != resolution.branch:
                raise DriftError(
                    t"Saved PR tracking for {ui.change_id(revision.change_id)} names branch "
                    t"{ui.bookmark(identity.head_ref)}, not "
                    t"{ui.bookmark(resolution.branch)}.",
                    condition="saved_pull_request_mismatch",
                    hint=t"Run {ui.cmd('relink')} before submitting again.",
                )
            if remote_target is None:
                raise DriftError(
                    t"Remote review branch "
                    t"{ui.bookmark(f'{resolution.branch}@{remote.name}')} no longer exists.",
                    condition="remote_branch_missing",
                    hint=(
                        t"Restore the branch, or use {ui.cmd('submit --restart')} to create "
                        t"a replacement review."
                    ),
                )
            if remote_target not in {baseline.commit_id, revision.commit_id}:
                raise DriftError(
                    t"Remote review branch "
                    t"{ui.bookmark(f'{resolution.branch}@{remote.name}')} points to an "
                    t"unexpected commit.",
                    condition="remote_branch_moved",
                    hint=(
                        t"Inspect it with {ui.cmd('view --fetch')} and repair the review "
                        t"before submitting again."
                    ),
                )
        elif baseline is not None:
            raise CliError(
                t"Saved PR tracking for {ui.change_id(revision.change_id)} has a last "
                t"submitted commit but no pull request identity.",
                hint=t"Repair it with {ui.cmd('relink')} before submitting again.",
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
        elif remote_target not in {None, revision.commit_id}:
            raise DriftError(
                t"Remote review branch "
                t"{ui.bookmark(f'{resolution.branch}@{remote.name}')} already exists and "
                t"points to another change.",
                condition="remote_branch_moved",
                hint="Move or delete the conflicting branch, then retry.",
            )

        prepared.append(
            PreparedSubmitRevision(
                branch=resolution.branch,
                expected_remote_target=remote_target,
                remote_action=("up to date" if remote_target == revision.commit_id else "pushed"),
                revision=revision,
            )
        )
    return tuple(prepared)
