"""Merge preconditions checked against fresh review observations."""

from __future__ import annotations

from collections.abc import Mapping

from jj_stack.commands.merge.models import MergeRevision
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.review.observation import RepositoryObservation, ReviewObservation


def merge_precondition_error(
    *,
    expected_bases: Mapping[str, tuple[str, ...]],
    expected_repository: GithubRepoAddress,
    expected_trunk_branch: str,
    expected_trunk_commit_id: str,
    observation: RepositoryObservation,
    remote_name: str,
    revisions: tuple[MergeRevision, ...],
    inactive_allowed: frozenset[str] = frozenset(),
) -> str | None:
    """Explain why fresh facts do not permit the next mutation."""

    remote = observation.remote
    if remote is None or remote.name != remote_name:
        return f"Git remote {remote_name} is no longer configured"
    if observation.configured_repository != expected_repository:
        return "the configured Git remote no longer names the planned GitHub repository"
    github_repository = observation.github_repository
    assert github_repository is not None
    if github_repository.full_name.casefold() != expected_repository.full_name.casefold():
        return "GitHub no longer reports the planned repository"
    if github_repository.default_branch not in (None, "", expected_trunk_branch):
        return "GitHub no longer reports the planned trunk branch as its default"
    if any(
        revision.change_id in observation.duplicate_claim_change_ids for revision in revisions
    ):
        return "multiple saved changes now claim the same pull request or review branch"
    if observation.fetched_trunk_commit_id != expected_trunk_commit_id:
        return "fetched trunk changed after planning"
    if observation.remote_trunk_target != expected_trunk_commit_id:
        return "the live trunk ref moved after planning"

    for revision in revisions:
        error = _review_precondition_error(
            expected_bases=expected_bases.get(revision.change_id, ()),
            expected_repository=expected_repository,
            observed=observation.reviews[revision.change_id],
            planned=revision,
            inactive_allowed=revision.change_id in inactive_allowed,
        )
        if error is not None:
            return error
    return None


def _review_precondition_error(
    *,
    expected_bases: tuple[str, ...],
    expected_repository: GithubRepoAddress,
    observed: ReviewObservation,
    planned: MergeRevision,
    inactive_allowed: bool,
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
        not planned.identity.matches_pull_request(pull_request)
        or len(observed.head_pull_requests) != 1
        or observed.head_pull_requests[0].number != pull_request.number
        or pull_request.head.sha != planned.commit_id
    ):
        return f"the pull request or its head commit for {label} changed"
    if (pull_request.state != "open" and not inactive_allowed) or (
        expected_bases and pull_request.base.ref not in expected_bases
    ):
        return f"pull request #{pull_request.number} state or base branch changed"
    if pull_request.is_draft and not inactive_allowed:
        return f"pull request #{pull_request.number} is now a draft"
    return None
