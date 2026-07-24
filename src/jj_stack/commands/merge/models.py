"""Shared data structures for the merge command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.review.status import PreparedStatus
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
    applied: bool
    blocked: bool
    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str
    merged_change_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedMerge:
    """Locally prepared merge inputs before GitHub planning and execution."""

    dry_run: bool
    context: CommandContext
    merge_method: str | None
    prepared_status: PreparedStatus


@dataclass(frozen=True, slots=True)
class MergeExecutionInputs:
    """Mutation dependencies independent of normal stack/status preparation."""

    context: CommandContext
    native_stacks: GithubStackSelection


@dataclass(frozen=True, slots=True)
class MergeRevision:
    """One selected change plus its GitHub link."""

    base_ref: str
    change_id: str
    commit_id: str
    identity: ReviewIdentity
    subject: str


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Resolved merge plan for the selected stack."""

    blocked: bool
    boundary_action: MergeAction | None
    planned_revisions: tuple[MergeRevision, ...]
    trunk_branch: str

    def planned_actions(self) -> tuple[MergeAction, ...]:
        if self.blocked:
            return () if self.boundary_action is None else (self.boundary_action,)

        actions = [
            MergeAction(
                kind="pull request",
                body=t"merge PR #{revision.identity.pr_number} into "
                t"{ui.bookmark(self.trunk_branch)} on GitHub for "
                t"{revision.subject} {ui.change_id(revision.change_id)}",
                status="planned",
            )
            for revision in self.planned_revisions
        ]
        if self.boundary_action is not None:
            actions.append(self.boundary_action)
        return tuple(actions)
