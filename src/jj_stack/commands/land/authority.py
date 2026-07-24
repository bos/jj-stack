"""Thin land policy over fresh, policy-free review observations."""

from __future__ import annotations

from collections.abc import Mapping

from jj_stack.commands.land.models import LandRevision
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.review.landing_authority import delegated_landing_mutation_error
from jj_stack.review.observation import RepositoryObservation, ReviewObservation


def land_authority_error(
    *,
    bypass_readiness: bool,
    expected_bases: Mapping[str, tuple[str, ...]],
    expected_repository: GithubRepoAddress,
    expected_trunk_branch: str,
    expected_trunk_commit_id: str,
    observation: RepositoryObservation,
    remote_name: str,
    revisions: tuple[LandRevision, ...],
) -> str | None:
    """Explain why fresh facts do not authorize one immediately following mutation."""

    remote = observation.remote
    if remote is None or remote.name != remote_name:
        return f"Git remote {remote_name} is no longer configured"
    if observation.configured_repository != expected_repository:
        return "the configured Git remote no longer names the planned GitHub repository"
    if (
        observation.github_repository.full_name.casefold()
        != expected_repository.full_name.casefold()
    ):
        return "GitHub no longer reports the planned repository"
    if observation.github_repository.default_branch not in (None, "", expected_trunk_branch):
        return "GitHub no longer reports the planned trunk branch as its default"
    if any(
        revision.change_id in observation.duplicate_claim_change_ids for revision in revisions
    ):
        return "multiple saved changes now claim the same pull request or review branch"
    fetched_trunk = observation.fetched_trunk
    if fetched_trunk is None or fetched_trunk.commit_id != expected_trunk_commit_id:
        return "fetched trunk changed after planning"
    if observation.remote_trunk_target != expected_trunk_commit_id:
        return "the live trunk ref moved after planning"

    for revision in revisions:
        error = _review_authority_error(
            bypass_readiness=bypass_readiness,
            expected_bases=expected_bases.get(revision.change_id, ()),
            expected_repository=expected_repository,
            observed=observation.reviews[revision.change_id],
            planned=revision,
        )
        if error is not None:
            return error
    return None


def _review_authority_error(
    *,
    bypass_readiness: bool,
    expected_bases: tuple[str, ...],
    expected_repository: GithubRepoAddress,
    observed: ReviewObservation,
    planned: LandRevision,
) -> str | None:
    identity = observed.identity
    local = observed.local_revision
    label = planned.change_id
    if (
        identity != planned.identity
        or identity is None
        or identity.repository_key != expected_repository.repository_key
    ):
        return f"saved PR tracking for {label} changed"
    if (
        observed.baseline is None
        or observed.baseline.commit_id != planned.commit_id
        or local is None
        or local.commit_id != planned.commit_id
        or local.conflict
        or local.divergent
        or observed.remote_review_target != planned.commit_id
    ):
        return f"the last submitted commit for {label} changed"
    pull_request = observed.pull_request
    if pull_request is None:
        return f"GitHub no longer reports the saved pull request for {label}"
    pull_request = pull_request.normalize_state()
    if (
        pull_request.number != planned.identity.pr_number
        or len(observed.head_pull_requests) != 1
        or observed.head_pull_requests[0].number != pull_request.number
        or pull_request.head.label != f"{identity.head_owner}:{identity.head_ref}"
        or pull_request.head.ref != identity.head_ref
        or pull_request.head.sha != planned.commit_id
    ):
        return f"the pull request or its head commit for {label} changed"
    if pull_request.state != "open" or (
        expected_bases and pull_request.base.ref not in expected_bases
    ):
        return f"pull request #{pull_request.number} state or base branch changed"
    if error := delegated_landing_mutation_error((pull_request,)):
        return error
    if pull_request.is_draft or (
        not bypass_readiness and pull_request.review_decision != "approved"
    ):
        return f"pull request #{pull_request.number} is no longer ready"
    return None
