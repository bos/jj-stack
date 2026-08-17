"""Derived per-change pull request lifecycle classification.

This module centralizes the observational state that commands derive from the local `jj` stack,
saved tracking data, remote-ref observations, and GitHub PR lookups. It deliberately does not
mutate tracking state or decide command policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import PRIdentity, TrackingState

if TYPE_CHECKING:
    from jj_stack.stack.status import PRLookup, StackStatusChange

LocalTrackingState = Literal["present", "divergent", "orphaned", "missing"]
PRLifecycle = Literal[
    "none",
    "open",
    "closed",
    "merged",
    "missing",
    "ambiguous",
]
PRReviewDecision = Literal[
    "none",
    "approved",
    "changes_requested",
    "commented",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ChangeStatus:
    """Orthogonal local and pull request status axes for one logical change."""

    local: LocalTrackingState
    pr_lifecycle: PRLifecycle
    pr_draft: bool | None
    pr_queued: bool | None
    pr_review_decision: PRReviewDecision
    pr_lookup_error: bool = False
    pr_review_decision_error: str | None = None
    saved_pr_identity: bool = False

    @property
    def has_pr_lookup_failure(self) -> bool:
        """Whether GitHub PR inspection failed for this change."""

        return self.pr_lookup_error or self.pr_review_decision_error is not None

    @property
    def has_stale_pr_link(self) -> bool:
        """Whether saved PR identity exists but live branch lookup is missing."""

        return self.pr_lifecycle == "missing" and self.saved_pr_identity

    @property
    def makes_report_incomplete(self) -> bool:
        """Whether this change stops `view` and `list` from reporting it completely.

        Both report commands share this rule so the same repo cannot yield a complete
        report from one and an incomplete report from the other.
        """

        return (
            (self.local == "divergent" and self.pr_lifecycle != "merged")
            or self.pr_lifecycle == "ambiguous"
            or self.has_pr_lookup_failure
            or self.has_stale_pr_link
        )


@dataclass(frozen=True, slots=True)
class OrphanedRecord:
    """A saved tracking record whose change has left every live stack."""

    change_id: str
    pr_identity: PRIdentity


def classify_stack_status_change(
    change: StackStatusChange,
) -> ChangeStatus:
    """Classify a rendered status change without performing I/O."""

    local: LocalTrackingState = "divergent" if change.local_divergent else "present"
    return classify_change_status(
        local=local,
        pr_lookup=change.pr_lookup,
        pr_identity=change.pr_identity,
    )


def classify_change_status(
    *,
    local: LocalTrackingState,
    pr_lookup: PRLookup | None,
    pr_identity: PRIdentity | None = None,
) -> ChangeStatus:
    """Derive change status axes from already-loaded facts."""

    lifecycle, pr_lookup_error = _pr_lifecycle(pr_lookup)
    return ChangeStatus(
        local=local,
        pr_lifecycle=lifecycle,
        pr_draft=_pr_draft(
            lifecycle=lifecycle,
            pr_lookup=pr_lookup,
        ),
        pr_queued=(
            pr_lookup.pr.is_queued
            if lifecycle == "open" and pr_lookup is not None and pr_lookup.pr is not None
            else None
        ),
        pr_review_decision=_pr_review_decision(
            lifecycle=lifecycle,
            pr_lookup=pr_lookup,
        ),
        pr_lookup_error=pr_lookup_error,
        pr_review_decision_error=(None if pr_lookup is None else pr_lookup.review_decision_error),
        saved_pr_identity=pr_identity is not None,
    )


def enumerate_orphaned_records(
    state: TrackingState,
    local_stacks: Sequence[LocalStack],
) -> tuple[OrphanedRecord, ...]:
    """Return saved PR records whose change is no longer in any live stack."""

    live_change_ids: set[str] = set()
    for stack in local_stacks:
        for change in stack.changes:
            live_change_ids.add(change.change_id)

    orphans: list[OrphanedRecord] = []
    for change_id, pr_identity in state.pr_identities.items():
        if change_id in live_change_ids:
            continue
        orphans.append(OrphanedRecord(change_id=change_id, pr_identity=pr_identity))
    return tuple(orphans)


def submitted_state_disagreement(
    state: TrackingState,
    local_stacks: Sequence[LocalStack],
) -> tuple[str, ...]:
    """Return change_ids whose saved submit baseline no longer matches the DAG."""

    disagreements: list[str] = []
    for stack in local_stacks:
        for change in stack.changes:
            pr_identity = state.pr_identities.get(change.change_id)
            if pr_identity is None:
                continue
            if state.submitted_baselines[change.change_id].commit_id != change.commit_id:
                disagreements.append(change.change_id)
    return tuple(disagreements)


def _pr_lifecycle(
    pr_lookup: PRLookup | None,
) -> tuple[PRLifecycle, bool]:
    if pr_lookup is None:
        return "none", False
    lookup_state = pr_lookup.state
    if lookup_state == "open":
        return "open", False
    if lookup_state == "closed":
        pr = pr_lookup.pr
        if pr is not None and pr.state == "merged":
            return "merged", False
        return "closed", False
    if lookup_state == "missing":
        return "missing", False
    if lookup_state == "ambiguous":
        return "ambiguous", False
    if lookup_state == "error":
        return "none", True
    return "none", True


def _pr_draft(
    *,
    lifecycle: PRLifecycle,
    pr_lookup: PRLookup | None,
) -> bool | None:
    if lifecycle != "open" or pr_lookup is None:
        return None
    pr = pr_lookup.pr
    if pr is None:
        return None
    return pr.is_draft


def _pr_review_decision(
    *,
    lifecycle: PRLifecycle,
    pr_lookup: PRLookup | None,
) -> PRReviewDecision:
    if lifecycle != "open" or pr_lookup is None:
        return "none"
    if pr_lookup.review_decision_error is not None:
        return "unknown"
    decision = pr_lookup.review_decision
    if decision is None:
        return "none"
    if decision in {"approved", "changes_requested", "commented"}:
        return decision
    return "unknown"
