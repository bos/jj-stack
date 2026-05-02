"""Output rendering for the land command."""

from __future__ import annotations

from jj_review import console, ui
from jj_review.ui import Message

from .models import LandAction, LandActionStatus, LandResult


def print_land_result(result: LandResult) -> None:
    console.output(t"Trunk: {result.trunk_subject} -> {ui.bookmark(result.trunk_branch)}")
    if result.actions:
        if result.applied:
            header = "Applied land actions:"
        elif result.blocked:
            header = "Land blocked:"
        else:
            header = "Planned land actions:"
        console.output(header)
        for action in result.actions:
            prefix, prefix_style, body_style = _land_action_presentation(action.status)
            console.output(
                ui.prefixed_line(
                    f"{prefix} ",
                    (_render_land_action_label(action), ": ", action.body),
                    prefix_labels=prefix_style,
                    message_labels=body_style,
                )
            )


def _land_action_presentation(
    status: LandActionStatus,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    if status == "applied":
        return (
            "  ✓",
            ("signature status good",),
            None,
        )
    if status == "planned":
        return (
            "  ~",
            ("hint heading",),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ("error heading",),
            ("warning heading",),
        )
    return ("  ?", None, None)


def _render_land_action_label(action: LandAction) -> Message:
    if action.kind == "boundary":
        return ui.semantic_text("stop", "prefix")
    return ui.semantic_text(action.kind, "prefix")
