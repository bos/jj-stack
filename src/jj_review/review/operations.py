"""Review-layer operation matching, display, and retirement policy."""

from __future__ import annotations

from typing import Literal

from jj_review import ui
from jj_review.state.journal import (
    CleanupOperationRecord,
    CleanupRebaseOperationRecord,
    CloseOperationRecord,
    LandOperationRecord,
    OperationRecord,
    RelinkOperationRecord,
    SubmitOperationRecord,
)
from jj_review.ui import Message

MatchResult = Literal["exact", "superset", "overlap", "disjoint"]
OrderedOperationMatch = Literal[
    "exact",
    "same-logical",
    "covered",
    "trimmed",
    "overlap",
    "disjoint",
]
CloseOperationModeRelation = Literal["same", "expanded", "incompatible"]


def match_ordered_change_ids(
    existing: tuple[str, ...],
    new: tuple[str, ...],
) -> MatchResult:
    """Classify how one ordered stack relates to another."""

    if existing == new:
        return "exact"
    if len(new) > len(existing) and new[: len(existing)] == existing:
        return "superset"
    if set(existing) & set(new):
        return "overlap"
    return "disjoint"


def describe_operation(
    operation: OperationRecord,
) -> Message:
    """Return a user-facing description for an interrupted operation."""

    if isinstance(operation, SubmitOperationRecord):
        return (
            t"{ui.cmd('submit')} for {_render_recorded_stack_head(operation)} "
            t"(from {ui.revset(operation.display_revset)})"
        )
    if isinstance(operation, CleanupRebaseOperationRecord):
        return (
            t"{ui.cmd('cleanup --rebase')} for {_render_recorded_stack_head(operation)} "
            t"(from {ui.revset(operation.display_revset)})"
        )
    if isinstance(operation, CloseOperationRecord):
        verb = ui.cmd("close --cleanup" if operation.cleanup else "close")
        return (
            t"{verb} for {_render_recorded_stack_head(operation)} "
            t"(from {ui.revset(operation.display_revset)})"
        )
    if isinstance(operation, LandOperationRecord):
        return (
            t"{ui.cmd('land')} for {_render_recorded_stack_head(operation)} "
            t"(from {ui.revset(operation.display_revset)})"
        )
    if isinstance(operation, RelinkOperationRecord):
        return t"{ui.cmd('relink')} for {ui.change_id(operation.change_id)}"
    if isinstance(operation, CleanupOperationRecord):
        return ui.cmd("cleanup")
    return operation.label


def _render_recorded_stack_head(
    operation: (
        LandOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | SubmitOperationRecord
    ),
) -> Message:
    if not operation.ordered_change_ids:
        return "stack"
    return ui.change_id(operation.ordered_change_ids[-1])


def match_cleanup_rebase_operation(
    *,
    operation: CleanupRebaseOperationRecord,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> OrderedOperationMatch:
    """Classify how a recorded cleanup rebase operation relates to the current stack."""

    if operation.ordered_change_ids == current_change_ids:
        if operation.ordered_commit_ids and operation.ordered_commit_ids == current_commit_ids:
            return "exact"
        return "same-logical"
    if set(operation.ordered_change_ids) == set(current_change_ids):
        return "same-logical"
    if set(current_change_ids).issubset(operation.ordered_change_ids):
        return "trimmed"
    return _match_recorded_ordered_stack(
        recorded_change_ids=operation.ordered_change_ids,
        recorded_commit_ids=operation.ordered_commit_ids,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def match_close_operation(
    *,
    operation: CloseOperationRecord,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
    current_cleanup: bool | None = None,
) -> OrderedOperationMatch:
    """Classify how a recorded close operation relates to the current stack."""

    if (
        current_cleanup is not None
        and close_operation_mode_relation(
            recorded_cleanup=operation.cleanup,
            current_cleanup=current_cleanup,
        )
        == "incompatible"
    ):
        return "disjoint"
    return _match_recorded_ordered_stack(
        recorded_change_ids=operation.ordered_change_ids,
        recorded_commit_ids=operation.ordered_commit_ids,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def close_operation_mode_relation(
    *,
    recorded_cleanup: bool,
    current_cleanup: bool,
) -> CloseOperationModeRelation:
    """Classify whether a close mode can resume or supersede a recorded close."""

    if recorded_cleanup == current_cleanup:
        return "same"
    if current_cleanup and not recorded_cleanup:
        return "expanded"
    return "incompatible"


def _match_recorded_ordered_stack(
    *,
    recorded_change_ids: tuple[str, ...],
    recorded_commit_ids: tuple[str, ...],
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> OrderedOperationMatch:
    """Classify how a recorded ordered stack relates to the current stack."""

    if recorded_change_ids == current_change_ids:
        if recorded_commit_ids and recorded_commit_ids == current_commit_ids:
            return "exact"
        return "same-logical"
    if set(recorded_change_ids).issubset(current_change_ids):
        return "covered"
    if set(recorded_change_ids) & set(current_change_ids):
        return "overlap"
    return "disjoint"
