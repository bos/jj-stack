"""Shared data structures for the merge command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jj_stack.bootstrap import CommandContext
from jj_stack.models.tracking import PRIdentity
from jj_stack.stack.status import PreparedStatus
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class MergeAction:
    """One planned, applied, or blocked merge action."""

    kind: str
    body: Message
    status: Literal["applied", "blocked", "planned"]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Rendered merge result for one selected local stack."""

    actions: tuple[MergeAction, ...]
    enqueued: bool
    selected_revset: str
    trunk_branch: str
    trunk_subject: str
    final_trunk_commit_id: str | None = None

    @property
    def applied(self) -> bool:
        return any(action.status == "applied" for action in self.actions)

    @property
    def blocked(self) -> bool:
        return any(action.status == "blocked" for action in self.actions)


@dataclass(frozen=True, slots=True)
class PreparedMerge:
    """Locally prepared merge inputs before GitHub planning and execution."""

    dry_run: bool
    context: CommandContext
    merge_method: str | None
    prepared_status: PreparedStatus
    target_change_id: str | None


@dataclass(frozen=True, slots=True)
class MergeExecutionInputs:
    """Mutation dependencies independent of normal stack/status preparation."""

    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str

    def result(
        self,
        *,
        actions: tuple[MergeAction, ...],
        enqueued: bool = False,
        final_trunk_commit_id: str | None = None,
    ) -> MergeResult:
        return MergeResult(
            actions=actions,
            enqueued=enqueued,
            final_trunk_commit_id=final_trunk_commit_id,
            selected_revset=self.selected_revset,
            trunk_branch=self.trunk_branch,
            trunk_subject=self.trunk_subject,
        )


@dataclass(frozen=True, slots=True)
class MergeChange:
    """One selected change plus its GitHub link."""

    base_ref: str
    change_id: str
    commit_id: str
    identity: PRIdentity
    subject: str


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Resolved merge plan for the selected stack."""

    boundary_action: MergeAction | None
    planned_changes: tuple[MergeChange, ...]
    linked_changes: tuple[MergeChange, ...]
