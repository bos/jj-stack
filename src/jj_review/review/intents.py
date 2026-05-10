"""Review-layer intent matching, display, and retirement policy."""

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
SubmitIntentMatch = Literal[
    "exact",
    "same-logical",
    "covered",
    "trimmed",
    "overlap",
    "disjoint",
]
CloseIntentModeRelation = Literal["same", "expanded", "incompatible"]


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


def describe_intent(
    intent: OperationRecord,
) -> Message:
    """Return a user-facing description for an interrupted operation."""

    if isinstance(intent, SubmitOperationRecord):
        return (
            t"{ui.cmd('submit')} for {_render_recorded_stack_head(intent)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, CleanupRebaseOperationRecord):
        return (
            t"{ui.cmd('cleanup --rebase')} for {_render_recorded_stack_head(intent)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, CloseOperationRecord):
        verb = ui.cmd("close --cleanup" if intent.cleanup else "close")
        return (
            t"{verb} for {_render_recorded_stack_head(intent)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, LandOperationRecord):
        return (
            t"{ui.cmd('land')} for {_render_recorded_stack_head(intent)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, RelinkOperationRecord):
        return t"{ui.cmd('relink')} for {ui.change_id(intent.change_id)}"
    if isinstance(intent, CleanupOperationRecord):
        return ui.cmd("cleanup")
    return intent.label


def _render_recorded_stack_head(
    intent: (
        LandOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | SubmitOperationRecord
    ),
) -> Message:
    if not intent.ordered_change_ids:
        return "stack"
    return ui.change_id(intent.ordered_change_ids[-1])


def match_cleanup_rebase_intent(
    *,
    intent: CleanupRebaseOperationRecord,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> SubmitIntentMatch:
    """Classify how a recorded cleanup rebase intent relates to the current stack."""

    if intent.ordered_change_ids == current_change_ids:
        if intent.ordered_commit_ids and intent.ordered_commit_ids == current_commit_ids:
            return "exact"
        return "same-logical"
    if set(intent.ordered_change_ids) == set(current_change_ids):
        return "same-logical"
    if set(current_change_ids).issubset(intent.ordered_change_ids):
        return "trimmed"
    return _match_recorded_ordered_stack(
        recorded_change_ids=intent.ordered_change_ids,
        recorded_commit_ids=intent.ordered_commit_ids,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def match_close_intent(
    *,
    intent: CloseOperationRecord,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
    current_cleanup: bool | None = None,
) -> SubmitIntentMatch:
    """Classify how a recorded close operation relates to the current stack."""

    if (
        current_cleanup is not None
        and close_intent_mode_relation(
            recorded_cleanup=intent.cleanup,
            current_cleanup=current_cleanup,
        )
        == "incompatible"
    ):
        return "disjoint"
    return _match_recorded_ordered_stack(
        recorded_change_ids=intent.ordered_change_ids,
        recorded_commit_ids=intent.ordered_commit_ids,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def close_intent_mode_relation(
    *,
    recorded_cleanup: bool,
    current_cleanup: bool,
) -> CloseIntentModeRelation:
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
) -> SubmitIntentMatch:
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
