"""Review-layer intent matching, display, and retirement policy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from jj_review import ui
from jj_review.models.intent import (
    CleanupIntent,
    CleanupRebaseIntent,
    CloseIntent,
    IntentFile,
    LoadedIntent,
    MatchResult,
    OrderedChangeIdsIntent,
    RelinkIntent,
    SubmitIntent,
)
from jj_review.review.submit_recovery import should_retire_submit_after_submit
from jj_review.state.journal import LandOperationRecord
from jj_review.system import pid_is_alive
from jj_review.ui import Message

logger = logging.getLogger(__name__)

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


def describe_intent(intent: IntentFile | LandOperationRecord) -> Message:
    """Return a user-facing description for an interrupted operation."""

    if isinstance(intent, SubmitIntent):
        return (
            t"{ui.cmd('submit')} for {_render_recorded_stack_head(intent)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, CleanupRebaseIntent):
        return (
            t"{ui.cmd('cleanup --rebase')} for {_render_recorded_stack_head(intent)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, CloseIntent):
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
    if isinstance(intent, RelinkIntent):
        return t"{ui.cmd('relink')} for {ui.change_id(intent.change_id)}"
    return intent.label


def _render_recorded_stack_head(
    intent: OrderedChangeIdsIntent | LandOperationRecord,
) -> Message:
    if not intent.ordered_change_ids:
        return "stack"
    return ui.change_id(intent.ordered_change_ids[-1])


def match_cleanup_rebase_intent(
    *,
    intent: CleanupRebaseIntent,
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
    intent: CloseIntent,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
    current_cleanup: bool | None = None,
) -> SubmitIntentMatch:
    """Classify how a recorded close intent relates to the current stack."""

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


def intent_is_stale(
    intent: IntentFile,
    resolve_change_id: Callable[[str], bool],
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether an interrupted intent is now stale."""

    if isinstance(intent, CleanupIntent | RelinkIntent):
        if pid_is_alive(intent.pid):
            return False
        if now is None:
            now = datetime.now(UTC)
        try:
            started = datetime.fromisoformat(intent.started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            return True
        return (now - started).days >= 7

    ids = intent.change_ids()
    if not ids:
        return False
    return not any(resolve_change_id(cid) for cid in ids)


def retire_superseded_intents(
    stale_intents: list[LoadedIntent],
    new_intent: IntentFile,
) -> None:
    """Auto-retire stale intents that a later successful run has superseded."""

    if not isinstance(new_intent, SubmitIntent | CleanupRebaseIntent | CloseIntent):
        return

    new_ids = new_intent.ordered_change_ids
    for loaded in stale_intents:
        old = loaded.intent
        if isinstance(new_intent, SubmitIntent):
            if not isinstance(old, SubmitIntent):
                continue
            should_retire = should_retire_submit_after_submit(
                old_intent=old,
                new_intent=new_intent,
            )
        elif isinstance(new_intent, CloseIntent):
            if isinstance(old, CloseIntent):
                should_retire = close_intent_mode_relation(
                    recorded_cleanup=old.cleanup,
                    current_cleanup=new_intent.cleanup,
                ) != "incompatible" and set(old.ordered_change_ids).issubset(new_ids)
            else:
                continue
        elif isinstance(new_intent, CleanupRebaseIntent):
            if not isinstance(old, CleanupRebaseIntent):
                continue
            should_retire = bool(set(old.ordered_change_ids) & set(new_ids))
        if should_retire:
            loaded.path.unlink(missing_ok=True)
            logger.debug("Retired superseded intent %s", loaded.path.name)


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
