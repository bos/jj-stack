"""Planning for `submit --restart`."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.branches import restarted_review_branch


@dataclass(frozen=True, slots=True)
class RestartedReview:
    """One exact saved pair to replace after fresh review creation."""

    baseline: SubmittedBaseline
    change_id: str
    identity: ReviewIdentity
    new_branch: str


@dataclass(frozen=True, slots=True)
class RestartStateResult:
    """Shadow state and saved pairs used while creating fresh reviews."""

    restarted: tuple[RestartedReview, ...]
    state: ReviewState


def restart_state_for_stack(
    *,
    stack: LocalStack,
    state: ReviewState,
) -> RestartStateResult:
    """Plan deterministic fresh branches without changing durable tracking."""

    reserved_branches = {
        identity.head_ref: change_id for change_id, identity in state.review_identities.items()
    }
    identities = dict(state.review_identities)
    baselines = dict(state.submitted_baselines)
    restarted: list[RestartedReview] = []
    for revision in stack.revisions:
        if state.issues_for(revision.change_id):
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is malformed.",
                hint=t"Repair it with {ui.cmd('relink')} before restarting the review.",
            )
        identity = state.review_identities.get(revision.change_id)
        baseline = state.submitted_baselines.get(revision.change_id)
        if identity is None or baseline is None:
            raise CliError(
                t"Cannot restart {ui.change_id(revision.change_id)} without complete saved "
                t"PR tracking.",
                hint="Submit it normally first, or select only changes with existing reviews.",
            )
        new_branch = _restart_branch(
            identity=identity,
            reserved_branches=reserved_branches,
            revision=revision,
        )
        reserved_branches[new_branch] = revision.change_id
        identities.pop(revision.change_id)
        baselines.pop(revision.change_id)
        restarted.append(
            RestartedReview(
                baseline=baseline,
                change_id=revision.change_id,
                identity=identity,
                new_branch=new_branch,
            )
        )

    return RestartStateResult(
        restarted=tuple(restarted),
        state=ReviewState(
            review_identities=identities,
            submitted_baselines=baselines,
            record_issues=state.record_issues,
        ),
    )


def _restart_branch(
    *,
    identity: ReviewIdentity,
    reserved_branches: dict[str, str],
    revision: LocalRevision,
) -> str:
    try:
        candidate = restarted_review_branch(
            change_id=revision.change_id,
            previous_branch=identity.head_ref,
            previous_pull_request=identity.pr_number,
        )
    except ValueError as error:
        raise CliError(
            t"Saved review branch {ui.bookmark(identity.head_ref)} does not match change "
            t"{ui.change_id(revision.change_id)}.",
            hint=t"Repair the saved review with {ui.cmd('relink')} before restarting it.",
        ) from error

    claimant = reserved_branches.get(candidate)
    if claimant is not None and claimant != revision.change_id:
        raise CliError(
            t"Cannot restart {ui.change_id(revision.change_id)} with "
            t"{ui.bookmark(candidate)} because another saved review claims that branch."
        )
    return candidate
