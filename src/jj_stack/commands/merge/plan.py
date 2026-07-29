"""Planning helpers for the merge command."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.review_state import ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.landed_evidence import candidate_for_change
from jj_stack.review.observation import RepositoryObservation
from jj_stack.ui import Message

from .models import MergeAction, MergePlan, MergeRevision
from .preconditions import explain_precondition, merge_precondition_error


def build_merge_plan(
    *,
    observation: RepositoryObservation,
    remote_name: str,
    repository: GithubRepoAddress,
    revisions: tuple[LocalRevision, ...],
    state: ReviewState,
    target_change_id: str | None,
    trunk_branch: str,
    trunk_commit_id: str,
) -> MergePlan:
    reviewed = tuple(_reviewed_revision(observation, revision, state) for revision in revisions)
    candidates: list[MergeRevision] = []
    boundary: Message | None = None
    for local, revision in zip(revisions, reviewed, strict=True):
        if revision is None:
            boundary = _boundary(local, t"run {ui.cmd('relink')} before merging")
            break
        error = merge_precondition_error(
            expected_bases={},
            expected_repository=repository,
            expected_trunk_branch=trunk_branch,
            expected_trunk_commit_id=trunk_commit_id,
            observation=observation,
            remote_name=remote_name,
            revisions=(revision,),
        )
        if error is not None:
            boundary = _boundary(
                local,
                explain_precondition(
                    error,
                    change_id=revision.change_id,
                    sync_target=short_change_id(revisions[-1].change_id),
                ),
            )
            break
        candidates.append(revision)
    if target_change_id is not None:
        target = next(
            (
                index + 1
                for index, revision in enumerate(candidates)
                if revision.change_id == target_change_id
            ),
            0,
        )
        candidates = candidates[:target]
        boundary = (
            None
            if target
            else boundary
            or (
                "the selected PR is above the changes that can merge right now; run "
                "jj-stack view to see which PRs at the bottom of the stack are ready"
            )
        )
    action = (
        MergeAction(
            kind="boundary",
            body=boundary or "No changes on the selected stack can be merged.",
            status="blocked" if not candidates else "planned",
        )
        if boundary is not None or not candidates
        else None
    )
    return MergePlan(
        blocked=not candidates,
        boundary_action=action,
        planned_revisions=tuple(candidates),
        reviewed_revisions=tuple(revision for revision in reviewed if revision is not None),
        trunk_branch=trunk_branch,
    )


def validate_merge_plan_method(*, merge_method: str, plan: MergePlan) -> None:
    if merge_method == "rebase" and len(plan.planned_revisions) > 1:
        raise CliError(
            "A rebase merge cannot merge more than one ordinary PR at a time.",
            hint=t"Use {ui.cmd('--merge-method squash')} or merge one PR per run.",
        )


def _reviewed_revision(
    observation: RepositoryObservation,
    revision: LocalRevision,
    state: ReviewState,
) -> MergeRevision | None:
    candidate = candidate_for_change(state, revision.change_id)
    pull_request = observation.reviews[revision.change_id].pull_request
    if candidate is None or pull_request is None:
        return None
    return MergeRevision(
        base_ref=pull_request.base.ref,
        change_id=revision.change_id,
        commit_id=revision.commit_id,
        identity=candidate.review_identity,
        subject=revision.subject,
    )


def _boundary(revision: LocalRevision, reason: Message) -> Message:
    return (
        t"before {revision.subject} {ui.change_id(revision.change_id)} because ",
        reason,
    )
