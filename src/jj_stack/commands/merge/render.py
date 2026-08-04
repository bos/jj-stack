"""Output rendering for the merge command."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui

from .models import MergeResult


def print_merge_result(result: MergeResult) -> None:
    console.output(t"Trunk: {result.trunk_subject} -> {ui.bookmark(result.trunk_branch)}")
    if result.actions:
        console.output(_result_header(result))
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
    if result.final_trunk_commit_id is not None:
        console.output(
            t"GitHub reported final trunk commit {ui.commit_id(result.final_trunk_commit_id)}."
        )
    if result.enqueued:
        console.output("GitHub will merge them once the queue processes them.")


def _result_header(result: MergeResult) -> str:
    if result.enqueued:
        return "In merge queue:"
    if result.applied:
        return "Applied merge actions:"
    if result.blocked:
        return "Merge blocked:"
    return "Planned merge actions:"
