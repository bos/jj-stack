"""Shared data structures for the land command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.review.status import PreparedStatus
from jj_stack.ui import Message

LandVia = Literal["push", "merge"]


@dataclass(frozen=True, slots=True)
class LandAction:
    """One planned, applied, or blocked landing action."""

    kind: str
    body: Message
    status: Literal["applied", "blocked", "planned"]


@dataclass(frozen=True, slots=True)
class LandResult:
    """Rendered landing result for one selected local stack."""

    actions: tuple[LandAction, ...]
    applied: bool
    blocked: bool
    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str
    via: LandVia
    # Change IDs GitHub accepted through the merge transport; the caller
    # converges the local stack for them afterwards.
    merged_change_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedLand:
    """Locally prepared land inputs before GitHub planning and execution."""

    cleanup_bookmarks: bool
    dry_run: bool
    bypass_readiness: bool
    context: CommandContext
    merge_method: str | None
    prepared_status: PreparedStatus
    via: LandVia


@dataclass(frozen=True, slots=True)
class LandExecutionInputs:
    """Mutation dependencies independent of normal stack/status preparation."""

    bypass_readiness: bool
    cleanup_bookmarks: bool
    context: CommandContext


@dataclass(frozen=True, slots=True)
class LandRevision:
    """One landed change plus its GitHub link."""

    base_ref: str
    change_id: str
    commit_id: str
    identity: ReviewIdentity
    subject: str


@dataclass(frozen=True, slots=True)
class LandPlan:
    """Resolved landing plan for the selected stack."""

    blocked: bool
    boundary_action: LandAction | None
    # Revisions this run should land.
    planned_revisions: tuple[LandRevision, ...]
    trunk_branch: str
    via: LandVia

    def planned_actions(
        self,
        *,
        bookmark_cleanup_actions: dict[str, LandAction] | None = None,
    ) -> tuple[LandAction, ...]:
        if self.blocked:
            return () if self.boundary_action is None else (self.boundary_action,)

        actions: list[LandAction] = []
        bookmark_cleanup_by_change_id = bookmark_cleanup_actions or {}
        if self.planned_revisions:
            if self.via == "push":
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
                        t"merge PR #{landed_revision.identity.pr_number} into "
                        t"{ui.bookmark(self.trunk_branch)} on GitHub for "
                        t"{landed_revision.subject} "
                        t"{ui.change_id(landed_revision.change_id)}"
                    )
                else:
                    pull_request_body = (
                        t"finalize PR #{landed_revision.identity.pr_number} for "
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
                            body=t"remove tracking for landed {landed_revision.subject} "
                            t"{ui.change_id(landed_revision.change_id)}",
                            status="planned",
                        )
                    )
        if self.boundary_action is not None:
            actions.append(self.boundary_action)
        return tuple(actions)
