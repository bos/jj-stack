"""Render submit command output."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.formatting import (
    format_pr_label,
    render_commit_blocks,
    render_commit_lines,
)
from jj_stack.jj.client import JjClient
from jj_stack.models.stack import LocalCommit

from .models import SubmitResult, SubmittedChange


def print_submit_result(result: SubmitResult) -> None:
    """Print the final submit result."""

    client = result.client
    # Overlap the `jj log` subprocess startup cost before we print the final summary for large
    # stacks.
    with console.spinner(description="Rendering jj log"):
        prerendered_blocks = render_commit_blocks(
            client=client,
            changes=tuple(change.prepared.change for change in result.changes) + (result.trunk,),
        )
    if not result.changes:
        for line in _render_submit_trunk_lines(
            client=client,
            prerendered_lines=prerendered_blocks.get(result.trunk.commit_id),
            trunk=result.trunk,
        ):
            console.output(line, soft_wrap=True)
        console.note(
            "The selected stack has no changes to submit.",
            soft_wrap=True,
        )
        return

    if result.dry_run:
        console.note("Dry run: no local, remote, or GitHub changes applied.", soft_wrap=True)
        console.output("Planned changes:")
    else:
        console.output("Submitted changes:")
    for change in reversed(result.changes):
        for line in _render_submit_change_lines(
            client=client,
            prerendered_lines=prerendered_blocks.get(change.prepared.change.commit_id),
            change=change,
        ):
            console.output(line, soft_wrap=True)
    for line in _render_submit_trunk_lines(
        client=client,
        prerendered_lines=prerendered_blocks.get(result.trunk.commit_id),
        trunk=result.trunk,
    ):
        console.output(line, soft_wrap=True)
    if not result.dry_run:
        top_pr_url = result.changes[-1].pr_url
        if top_pr_url is not None:
            console.output(ui.prefixed_line("Top of stack: ", top_pr_url))


def print_selected_line(
    selected_change_id: str,
    selected_subject: str,
) -> None:
    """Print the selected stack head line."""

    console.output(
        ui.prefixed_line(
            "Selected: ",
            t"{selected_subject} ({ui.change_id(selected_change_id)})",
        )
    )


def _render_submit_change_lines(
    *,
    client: JjClient,
    prerendered_lines: tuple[str, ...] | None = None,
    change: SubmittedChange,
) -> tuple[ui.Renderable, ...]:
    parts: list[str] = []
    if change.pr_action != "created":
        if change.prepared.remote_action == "up to date":
            parts.append("already pushed")
        else:
            parts.append("pushed")

    if change.pr_number is None:
        if change.pr_action == "created":
            parts.append("new PR")
        elif change.pr_action == "updated":
            parts.append("PR updated")
        else:
            parts.append("PR unchanged")
    else:
        label = format_pr_label(
            change.pr_number,
            is_draft=bool(change.pr_is_draft),
        )
        if change.pr_action == "created":
            parts.append(label)
        else:
            parts.append(f"{label} {change.pr_action}")

    summary = ", ".join(parts)
    return render_commit_lines(
        client=client,
        prerendered_lines=prerendered_lines,
        change=change.prepared.change,
        suffix=summary,
    )


def _render_submit_trunk_lines(
    *,
    client: JjClient,
    prerendered_lines: tuple[str, ...] | None = None,
    trunk: LocalCommit,
) -> tuple[ui.Renderable, ...]:
    return render_commit_lines(
        client=client,
        prerendered_lines=prerendered_lines,
        change=trunk,
    )
