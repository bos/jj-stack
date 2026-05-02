"""Output rendering for the land command."""

from __future__ import annotations

from jj_review import console, ui

from .models import LandResult


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
            if action.status == "applied":
                prefix = "  ✓"
                prefix_style = ("signature status good",)
                body_style = None
            elif action.status == "planned":
                prefix = "  ~"
                prefix_style = ("hint heading",)
                body_style = None
            else:
                prefix = "  ✗"
                prefix_style = ("error heading",)
                body_style = ("warning heading",)
            action_label = "stop" if action.kind == "boundary" else action.kind
            console.output(
                ui.prefixed_line(
                    f"{prefix} ",
                    (ui.semantic_text(action_label, "prefix"), ": ", action.body),
                    prefix_labels=prefix_style,
                    message_labels=body_style,
                )
            )
