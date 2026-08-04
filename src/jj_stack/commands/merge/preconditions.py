"""Merge preconditions checked against fresh review observations."""

from __future__ import annotations

from collections.abc import Mapping

import jj_stack.ui as ui
from jj_stack.commands.merge.models import MergeRevision
from jj_stack.formatting import short_change_id
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.review.observation import RepositoryObservation, ReviewObservation
from jj_stack.ui import Message


def merge_precondition_error(
    *,
    expected_bases: Mapping[str, tuple[str, ...]],
    expected_repository: GithubRepoAddress,
    expected_trunk_branch: str,
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


def explain_precondition(reason: str, *, change_id: str, sync_target: str) -> Message:
    """Restate a precondition reason so it names the command that resolves it.

    Planning and execution both stop on these reasons, so they share one wording rather than each
    deciding what to tell the user.
    """

    # Every boundary already names the change, so these do not repeat its ID.
    submit = ui.cmd(f"jj-stack submit {short_change_id(change_id)}")
    if "unresolved conflicts" in reason:
        return t"it has unresolved conflicts; resolve them with jj, then run {submit}"
    if "more than one visible revision" in reason:
        return (
            t"it has more than one visible revision; reconcile them, for example with "
            t"{ui.cmd('jj log -r')} {ui.revset(f'change_id({short_change_id(change_id)})')}, "
            t"then run {submit}"
        )
    if "no longer visible locally" in reason:
        return (
            t"it is no longer visible locally; run {ui.cmd('jj-stack view')} to find where it "
            t"went, or {ui.cmd(f'jj-stack sync {sync_target}')} if it already merged"
        )
    if "last submitted commit" in reason:
        return (
            t"the local change, the commit last submitted for it, and its review branch do not "
            t"all name the same commit; run {submit}"
        )
    if "is already merged" in reason:
        return (
            t"{reason}, so this stack still holds a local copy of work already on trunk; run "
            t"{ui.cmd(f'jj-stack sync {sync_target}')}"
        )
    return t"{reason}; inspect it and rerun {ui.cmd('merge')}"


def _review_precondition_error(
    *,
    expected_bases: tuple[str, ...],
    expected_repository: GithubRepoAddress,
    observed: ReviewObservation,
    planned: MergeRevision,
    inactive_allowed: bool,
) -> str | None:
    return _local_precondition_error(
        expected_repository=expected_repository,
        observed=observed,
        planned=planned,
    ) or _pull_request_precondition_error(
        expected_bases=expected_bases,
        observed=observed,
        planned=planned,
        inactive_allowed=inactive_allowed,
    )


def _local_precondition_error(
    *,
    expected_repository: GithubRepoAddress,
    observed: ReviewObservation,
    planned: MergeRevision,
) -> str | None:
    """Explain why the local change and its review branch do not match the plan."""

    identity = observed.identity
    local_revisions = observed.local_revisions
    label = planned.change_id
    if (
        identity != planned.identity
        or identity is None
        or identity.repository_key != expected_repository.repository_key
    ):
        return f"saved PR tracking for {label} changed"
    if not local_revisions:
        return f"{label} is no longer visible locally"
    # Stack discovery normally rejects a divergent change first; this covers one that diverged
    # after the plan was built.
    if len(local_revisions) > 1 or local_revisions[0].divergent:
        return f"{label} has more than one visible revision"
    local = local_revisions[0]
    # Conflicts come before the commit comparison: a rebase that conflicts also changes the
    # commit, and resolving is what has to happen first either way.
    if local.conflict:
        return f"{label} has unresolved conflicts"
    if (
        observed.baseline is None
        or observed.baseline.commit_id != planned.commit_id
        or local.commit_id != planned.commit_id
        or observed.remote_review_target != planned.commit_id
    ):
        return f"the last submitted commit for {label} changed"
    return None


def _pull_request_precondition_error(
    *,
    expected_bases: tuple[str, ...],
    observed: ReviewObservation,
    planned: MergeRevision,
    inactive_allowed: bool,
) -> str | None:
    """Explain why GitHub's view of the pull request does not match the plan."""

    pull_request = observed.pull_request
    if pull_request is None:
        return f"GitHub no longer reports the saved pull request for {planned.change_id}"
    pull_request = pull_request.normalize_state()
    # The head commit is deliberately not compared here. GitHub is given the expected head with
    # the merge request and rejects a stale one atomically, which a check made beforehand cannot
    # do; the review branch is still compared against the submitted baseline above.
    if (
        not planned.identity.matches_pull_request(pull_request)
        or len(observed.head_pull_requests) != 1
        or observed.head_pull_requests[0].number != pull_request.number
    ):
        return f"the pull request linked to {planned.change_id} changed"
    if pull_request.state == "merged" and not inactive_allowed:
        return f"pull request #{pull_request.number} is already merged"
    if (pull_request.state != "open" and not inactive_allowed) or (
        expected_bases and pull_request.base.ref not in expected_bases
    ):
        return f"pull request #{pull_request.number} state or base branch changed"
    if pull_request.is_draft and not inactive_allowed:
        return f"pull request #{pull_request.number} is now a draft"
    return None
