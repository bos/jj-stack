"""Shared reporting meaning for classified changes; commands only format and aggregate it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.identifiers import ChangeId
from jj_stack.models.github import CheckRollupStatus, GithubPR
from jj_stack.stack.change_state import (
    BranchClaimed,
    BranchDisagrees,
    BranchMissing,
    ChangeState,
    Closed,
    CompetingOpenPR,
    Edited,
    Landed,
    LookupFailed,
    Merged,
    NotInspected,
    PRAmbiguous,
    PRHeadMoved,
    PRIdentityMismatch,
    PRMissing,
    Published,
    PushedUnrecorded,
    Queued,
    Stop,
    Unpublished,
    UntrackedPRExists,
    WithPR,
)

type ReportStatus = Literal[
    "unsubmitted",
    "submitted",
    "open",
    "queued",
    "draft",
    "approved",
    "changes_requested",
    "merged",
    "closed",
    "missing",
    "ambiguous",
    "link_mismatch",
    "branch_moved",
    "divergent",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ChangeReport:
    lifecycle: ReportStatus
    problem: ReportStatus | None
    divergent: bool
    needs_sync: bool
    needs_submit: bool
    checks: CheckRollupStatus | None
    reason: ui.Message | None
    repair: ui.Message | None

    @property
    def status(self) -> ReportStatus:
        return "divergent" if self.divergent else self.problem or self.lifecycle


def report_change(state: ChangeState) -> ChangeReport:
    """Interpret every classified state without falling back to a healthy PR for a stop."""

    problem: ReportStatus | None = None
    match state:
        case Unpublished() | UntrackedPRExists() | BranchClaimed():
            lifecycle: ReportStatus = "unsubmitted"
        case NotInspected():
            lifecycle = "submitted"
        case LookupFailed():
            lifecycle = "submitted" if state.tracked is not None else "unsubmitted"
            problem = "unknown"
        case PRMissing() | PRAmbiguous():
            lifecycle = "submitted"
            problem = "missing" if isinstance(state, PRMissing) else "ambiguous"
        case Landed() | Merged():
            lifecycle = "merged"
        case (
            Published()
            | Edited()
            | PushedUnrecorded()
            | Queued()
            | Closed()
            | PRIdentityMismatch()
            | CompetingOpenPR()
            | PRHeadMoved()
            | BranchMissing()
            | BranchDisagrees()
        ):
            lifecycle = _pr_status(state.pr)
            if isinstance(state, PRIdentityMismatch):
                problem = "link_mismatch"
            elif isinstance(state, CompetingOpenPR):
                problem = "ambiguous" if state.ambiguous else "link_mismatch"
            elif isinstance(state, (PRHeadMoved, BranchDisagrees)):
                problem = "branch_moved"
            elif isinstance(state, BranchMissing):
                problem = "link_mismatch"
    needs_sync = isinstance(state, (Landed, Merged))
    divergent = state.divergent and not needs_sync
    return ChangeReport(
        lifecycle=lifecycle,
        problem=problem,
        divergent=divergent,
        needs_sync=needs_sync,
        needs_submit=(
            isinstance(state, (Edited, PushedUnrecorded))
            and state.has_local_edits
            and not divergent
        ),
        checks=(
            state.pr.check_rollup_status
            if isinstance(state, WithPR) and state.pr.state == "open"
            else None
        ),
        reason=state.reason if isinstance(state, Stop) else None,
        repair=state.repair if isinstance(state, Stop) else None,
    )


def submittable_edits(reports: Mapping[ChangeId, ChangeReport]) -> tuple[ChangeId, ...]:
    """Changes a `submit` would refresh; none when anything in the stack would stop `submit`."""

    blocked = any(
        report.repair is not None or report.divergent or report.lifecycle in ("closed", "queued")
        for report in reports.values()
    )
    if blocked:
        return ()
    return tuple(change_id for change_id, report in reports.items() if report.needs_submit)


def _pr_status(pr: GithubPR) -> ReportStatus:
    if pr.state == "merged":
        return "merged"
    if pr.state == "closed":
        return "closed"
    if pr.is_queued:
        return "queued"
    if pr.is_draft:
        return "draft"
    if pr.review_decision == "approved":
        return "approved"
    if pr.review_decision == "changes_requested":
        return "changes_requested"
    return "open"


_STATUS_LABELS: dict[ReportStatus, tuple[str, str, str | None]] = {
    "unsubmitted": ("not submitted", "not submitted", None),
    "submitted": ("submitted", "submitted", None),
    "open": ("open", "open", None),
    "queued": ("queued", "queued", "hint"),
    "draft": ("draft", "drafts", "hint"),
    "approved": ("approved", "approved", "hint"),
    "changes_requested": ("changes requested", "changes requested", "warning"),
    "merged": ("sync needed", "merged, sync needed", "warning"),
    "closed": ("closed", "closed", "warning"),
    "missing": ("missing PR", "missing PRs", "warning"),
    "ambiguous": ("ambiguous PR", "ambiguous PRs", "warning"),
    "link_mismatch": ("saved PR needs repair", "saved PRs need repair", "warning"),
    "branch_moved": ("PR branch moved", "PR branches moved", "warning"),
    "divergent": ("divergent", "divergent changes", "warning"),
    "unknown": ("GitHub lookup failed", "GitHub lookups failed", "warning"),
}


def status_label(status: ReportStatus, *, count: int = 1) -> ui.Message:
    singular, plural, severity = _STATUS_LABELS[status]
    label = singular if count == 1 else f"{count} {plural}"
    return label if severity is None else ui.semantic_text(label, severity, "heading")
