"""Review-layer operation matching, display, and retirement policy."""

from __future__ import annotations

from typing import Literal

from jj_review import ui
from jj_review.formatting import short_change_id
from jj_review.state.journal import (
    CleanupOperationRecord,
    CleanupRebaseOperationRecord,
    CloseOperationRecord,
    LandOperationRecord,
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


def operation_kind(operation: object) -> str:
    """Return the stable journal operation kind for diagnostics."""

    if isinstance(operation, LandOperationRecord):
        return "land"
    if isinstance(operation, SubmitOperationRecord):
        return "submit"
    if isinstance(operation, RelinkOperationRecord):
        return "relink"
    if isinstance(operation, CleanupOperationRecord):
        return "cleanup"
    if isinstance(operation, CleanupRebaseOperationRecord):
        return "cleanup-rebase"
    if isinstance(operation, CloseOperationRecord):
        return "close"
    return str(getattr(operation, "kind", "operation"))


def operation_command(operation: object) -> str:
    """Return the jj-review command name for an interrupted operation."""

    if isinstance(operation, SubmitOperationRecord):
        return "submit"
    if isinstance(operation, CleanupRebaseOperationRecord):
        return "cleanup --rebase"
    if isinstance(operation, CloseOperationRecord):
        return "close --cleanup" if operation.cleanup else "close"
    if isinstance(operation, LandOperationRecord):
        return "land"
    if isinstance(operation, RelinkOperationRecord):
        return "relink"
    if isinstance(operation, CleanupOperationRecord):
        return "cleanup"
    return str(getattr(operation, "label", "operation"))


def operation_selector(operation: object) -> str | None:
    """Return the short selector to rerun or inspect an interrupted operation."""

    if isinstance(
        operation,
        SubmitOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | LandOperationRecord,
    ):
        if operation.ordered_change_ids:
            return short_change_id(operation.ordered_change_ids[-1])
    if isinstance(operation, RelinkOperationRecord):
        return short_change_id(operation.change_id)
    return None


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


def describe_operation(operation: object) -> Message:
    """Return a user-facing description for an interrupted operation."""

    if isinstance(
        operation,
        SubmitOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | LandOperationRecord,
    ):
        command = ui.cmd(operation_command(operation))
        stack_head = _render_recorded_stack_head(operation)
        return (
            t"{command} for {stack_head} (from {ui.revset(operation.display_revset)})"
        )
    if isinstance(operation, RelinkOperationRecord):
        return t"{ui.cmd(operation_command(operation))} for {ui.change_id(operation.change_id)}"
    if isinstance(operation, CleanupOperationRecord):
        return ui.cmd(operation_command(operation))
    return str(getattr(operation, "label", "operation"))


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

    return match_stack_operation(
        recorded_change_ids=operation.ordered_change_ids,
        recorded_commit_ids=operation.ordered_commit_ids,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def match_land_operation(
    *,
    operation: LandOperationRecord,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> OrderedOperationMatch:
    """Classify how a recorded land operation relates to the current stack."""

    return match_stack_operation(
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


def match_stack_operation(
    *,
    recorded_change_ids: tuple[str, ...],
    recorded_commit_ids: tuple[str, ...],
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> OrderedOperationMatch:
    """Classify how a recorded stack-like operation relates to the current stack."""

    if recorded_change_ids == current_change_ids:
        if recorded_commit_ids and recorded_commit_ids == current_commit_ids:
            return "exact"
        return "same-logical"
    if set(recorded_change_ids) == set(current_change_ids):
        return "same-logical"
    if set(recorded_change_ids).issubset(current_change_ids):
        return "covered"
    if set(current_change_ids).issubset(recorded_change_ids):
        return "trimmed"
    if set(recorded_change_ids) & set(current_change_ids):
        return "overlap"
    return "disjoint"


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
