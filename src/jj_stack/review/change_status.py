"""Derived per-change review lifecycle classification.

This module centralizes the observational state that commands derive from the local `jj` stack,
saved tracking data, remote-ref observations, and GitHub PR lookups. It deliberately does not
mutate tracking state or decide command policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.models.stack import LocalStack

if TYPE_CHECKING:
    from jj_stack.review.status import PullRequestLookup, ReviewStatusRevision

LocalReviewState = Literal["present", "divergent", "orphaned", "missing"]
RemoteBranchReviewState = Literal[
    "absent",
    "current",
    "drifted",
]
PullRequestLifecycle = Literal[
    "none",
    "open",
    "closed",
    "merged",
    "missing",
    "ambiguous",
]
PullRequestReviewDecision = Literal[
    "none",
    "approved",
    "changes_requested",
    "commented",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ReviewChangeStatus:
    """Orthogonal review state axes for one logical change."""

    local: LocalReviewState
    remote_branch: RemoteBranchReviewState
    remote_branch_matches_commit: bool | None
    pr_lifecycle: PullRequestLifecycle
    pr_draft: bool | None
    pr_review_decision: PullRequestReviewDecision
    pr_lookup_error: bool = False
    pr_review_decision_error: str | None = None
    saved_review_identity: bool = False

    @property
    def has_pull_request_lookup_failure(self) -> bool:
        """Whether GitHub PR inspection failed for this change."""

        return self.pr_lookup_error or self.pr_review_decision_error is not None

    @property
    def has_stale_pull_request_link(self) -> bool:
        """Whether saved PR identity exists but live branch lookup is missing."""

        return self.pr_lifecycle == "missing" and self.saved_review_identity


@dataclass(frozen=True, slots=True)
class OrphanedRecord:
    """A saved tracking record whose change has left every live stack."""

    change_id: str
    review_identity: ReviewIdentity


def classify_review_status_revision(
    revision: ReviewStatusRevision,
) -> ReviewChangeStatus:
    """Classify a rendered status revision without performing I/O."""

    local: LocalReviewState = "divergent" if revision.local_divergent else "present"
    return classify_review_change(
        commit_id=revision.commit_id,
        local=local,
        pull_request_lookup=revision.pull_request_lookup,
        remote_target=revision.remote_target,
        review_identity=revision.review_identity,
    )


def classify_review_change(
    *,
    commit_id: str | None,
    local: LocalReviewState,
    pull_request_lookup: PullRequestLookup | None,
    remote_target: str | None,
    review_identity: ReviewIdentity | None = None,
) -> ReviewChangeStatus:
    """Derive review status axes from already-loaded observations."""

    lifecycle, pr_lookup_error = _pull_request_lifecycle(pull_request_lookup)
    return ReviewChangeStatus(
        local=local,
        remote_branch=_remote_branch_state(
            commit_id=commit_id,
            remote_target=remote_target,
        ),
        remote_branch_matches_commit=(
            None if commit_id is None or remote_target is None else remote_target == commit_id
        ),
        pr_lifecycle=lifecycle,
        pr_draft=_pull_request_draft(
            lifecycle=lifecycle,
            pull_request_lookup=pull_request_lookup,
        ),
        pr_review_decision=_pull_request_review_decision(
            lifecycle=lifecycle,
            pull_request_lookup=pull_request_lookup,
        ),
        pr_lookup_error=pr_lookup_error,
        pr_review_decision_error=(
            None if pull_request_lookup is None else pull_request_lookup.review_decision_error
        ),
        saved_review_identity=review_identity is not None,
    )


def classify_review_change_without_pull_request(
    *,
    commit_id: str | None,
    local: LocalReviewState = "present",
    remote_target: str | None,
    review_identity: ReviewIdentity | None = None,
) -> ReviewChangeStatus:
    """Classify review state when pull request data was not loaded."""

    return classify_review_change(
        commit_id=commit_id,
        local=local,
        pull_request_lookup=None,
        remote_target=remote_target,
        review_identity=review_identity,
    )


def classify_saved_review_identity(
    review_identity: ReviewIdentity | None,
    *,
    local: LocalReviewState = "missing",
) -> ReviewChangeStatus:
    """Classify one saved identity when live remote or PR data is not loaded."""

    return classify_review_change(
        commit_id=None,
        local=local,
        pull_request_lookup=None,
        remote_target=None,
        review_identity=review_identity,
    )


def enumerate_orphaned_records(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[OrphanedRecord, ...]:
    """Return saved review records whose change is no longer in any live stack."""

    live_change_ids: set[str] = set()
    for stack in local_stacks:
        for revision in stack.revisions:
            live_change_ids.add(revision.change_id)

    orphans: list[OrphanedRecord] = []
    for change_id, review_identity in state.review_identities.items():
        if change_id in live_change_ids:
            continue
        orphans.append(OrphanedRecord(change_id=change_id, review_identity=review_identity))
    return tuple(orphans)


def submitted_state_disagreement(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[str, ...]:
    """Return change_ids whose saved submit baseline no longer matches the DAG."""

    disagreements: list[str] = []
    for stack in local_stacks:
        for revision in stack.revisions:
            review_identity = state.review_identities.get(revision.change_id)
            if review_identity is None:
                continue
            baseline = state.submitted_baselines.get(revision.change_id)
            if baseline is not None and baseline.commit_id != revision.commit_id:
                disagreements.append(revision.change_id)
    return tuple(disagreements)


def _remote_branch_state(
    *,
    commit_id: str | None,
    remote_target: str | None,
) -> RemoteBranchReviewState:
    if remote_target is None:
        return "absent"
    if commit_id is not None and remote_target == commit_id:
        return "current"
    return "drifted"


def _pull_request_lifecycle(
    pull_request_lookup: PullRequestLookup | None,
) -> tuple[PullRequestLifecycle, bool]:
    if pull_request_lookup is None:
        return "none", False
    lookup_state = pull_request_lookup.state
    if lookup_state == "open":
        return "open", False
    if lookup_state == "closed":
        pull_request = pull_request_lookup.pull_request
        if pull_request is not None and pull_request.state == "merged":
            return "merged", False
        return "closed", False
    if lookup_state == "missing":
        return "missing", False
    if lookup_state == "ambiguous":
        return "ambiguous", False
    if lookup_state == "error":
        return "none", True
    return "none", True


def _pull_request_draft(
    *,
    lifecycle: PullRequestLifecycle,
    pull_request_lookup: PullRequestLookup | None,
) -> bool | None:
    if lifecycle != "open" or pull_request_lookup is None:
        return None
    pull_request = pull_request_lookup.pull_request
    if pull_request is None:
        return None
    return pull_request.is_draft


def _pull_request_review_decision(
    *,
    lifecycle: PullRequestLifecycle,
    pull_request_lookup: PullRequestLookup | None,
) -> PullRequestReviewDecision:
    if lifecycle != "open" or pull_request_lookup is None:
        return "none"
    if pull_request_lookup.review_decision_error is not None:
        return "unknown"
    decision = pull_request_lookup.review_decision
    if decision is None:
        return "none"
    if decision in {"approved", "changes_requested", "commented"}:
        return decision
    return "unknown"
