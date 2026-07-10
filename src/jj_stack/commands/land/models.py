"""Shared data structures for the land command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.review.status import PreparedStatus
from jj_stack.state.store import ReviewStateStore
from jj_stack.ui import Message, plain_text

LandVia = Literal["push", "merge"]


@dataclass(frozen=True, slots=True)
class LandAction:
    """One planned, applied, or blocked landing action."""

    kind: str
    body: Message
    status: Literal["applied", "blocked", "planned"]

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class LandResult:
    """Rendered landing result for one selected local stack."""

    actions: tuple[LandAction, ...]
    applied: bool
    bypass_readiness: bool
    blocked: bool
    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str
    via: LandVia


@dataclass(frozen=True, slots=True)
class PreparedLand:
    """Locally prepared land inputs before GitHub planning and execution."""

    cleanup_bookmarks: bool
    dry_run: bool
    bypass_readiness: bool
    context: CommandContext
    merge_method: str | None
    prepared_status: PreparedStatus
    selected_pr_number: int | None
    via: LandVia


@dataclass(slots=True)
class LandMutationRun:
    """Mutable land state shared by live execution phases."""

    state: ReviewState
    state_changes: dict[str, CachedChange]
    state_store: ReviewStateStore

    def save_interim_state(self) -> None:
        self.state_store.save(
            self.state.model_copy(update={"changes": dict(self.state_changes)})
        )


@dataclass(frozen=True, slots=True)
class LandRevision:
    """One landed change plus its GitHub link."""

    bookmark: str
    bookmark_managed: bool
    change_id: str
    commit_id: str
    needs_resubmit: bool
    pull_request_number: int
    subject: str


@dataclass(frozen=True, slots=True)
class LandPlan:
    """Resolved landing plan for the selected stack."""

    blocked: bool
    boundary_action: LandAction | None
    # Revisions this run should land.
    planned_revisions: tuple[LandRevision, ...]
    push_trunk: bool
    trunk_branch: str
    via: LandVia
    repair_local_trunk_commit_id: str | None = None
    resumed_operation_id: str | None = None

    @property
    def resubmit_revisions(self) -> tuple[LandRevision, ...]:
        return tuple(revision for revision in self.planned_revisions if revision.needs_resubmit)

    def planned_actions(
        self,
        *,
        bookmark_cleanup_plans: tuple[ReviewBookmarkCleanupPlan, ...] = (),
    ) -> tuple[LandAction, ...]:
        if self.blocked:
            return () if self.boundary_action is None else (self.boundary_action,)

        actions: list[LandAction] = []
        bookmark_cleanup_by_change_id = {
            cleanup_plan.change_id: cleanup_plan.action
            for cleanup_plan in bookmark_cleanup_plans
        }
        if self.planned_revisions:
            if self.repair_local_trunk_commit_id is not None:
                actions.append(
                    LandAction(
                        kind="local trunk",
                        body=t"move {ui.bookmark(self.trunk_branch)} to the current "
                        t"{ui.revset('trunk()')} after the interrupted push",
                        status="planned",
                    )
                )
            for resubmit_revision in self.resubmit_revisions:
                actions.append(
                    LandAction(
                        kind="review branch",
                        body=t"refresh {ui.bookmark(resubmit_revision.bookmark)} to match "
                        t"{resubmit_revision.subject} "
                        t"{ui.change_id(resubmit_revision.change_id)} before landing",
                        status="planned",
                    )
                )
            if self.push_trunk:
                actions.append(
                    LandAction(
                        kind="trunk",
                        body=t"push {ui.bookmark(self.trunk_branch)} to "
                        t"{self.planned_revisions[-1].subject} "
                        t"{ui.change_id(self.planned_revisions[-1].change_id)}",
                        status="planned",
                    )
                )
            for landed_revision in self.planned_revisions:
                if self.via == "merge":
                    pull_request_body = (
                        t"merge PR #{landed_revision.pull_request_number} into "
                        t"{ui.bookmark(self.trunk_branch)} on GitHub for "
                        t"{landed_revision.subject} "
                        t"{ui.change_id(landed_revision.change_id)}"
                    )
                else:
                    pull_request_body = (
                        t"finalize PR #{landed_revision.pull_request_number} for "
                        t"{landed_revision.subject} "
                        t"{ui.change_id(landed_revision.change_id)}"
                    )
                actions.append(
                    LandAction(
                        kind="pull request",
                        body=pull_request_body,
                        status="planned",
                    )
                )
                cleanup_action = bookmark_cleanup_by_change_id.get(landed_revision.change_id)
                if cleanup_action is not None:
                    actions.append(cleanup_action)
                if self.via == "push":
                    actions.append(
                        LandAction(
                            kind="tracking",
                            body=landed_tracking_retire_body(landed_revision),
                            status="planned",
                        )
                    )
        if self.boundary_action is not None:
            actions.append(self.boundary_action)
        return tuple(actions)

    def completed_actions(self, *, actions: tuple[LandAction, ...]) -> tuple[LandAction, ...]:
        if self.boundary_action is None:
            return actions
        return (*actions, self.boundary_action)


@dataclass(frozen=True, slots=True)
class ReviewBookmarkCleanupPlan:
    """Planned post-land cleanup for one landed local review bookmark."""

    action: LandAction
    bookmark: str
    can_forget: bool
    change_id: str


def landed_tracking_retire_body(landed_revision: LandRevision) -> Message:
    """Render the direct-push tracking cleanup action for a landed revision."""

    return (
        t"remove tracking for landed {landed_revision.subject} "
        t"{ui.change_id(landed_revision.change_id)}"
    )


class BookmarkStateReader(Protocol):
    """Subset of the jj client interface needed for trunk bookmark inspection."""

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        """Return local and remote state for the named bookmark."""
