"""Render submit command output."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.formatting import (
    format_pull_request_label,
    render_revision_blocks,
    render_revision_lines,
)
from jj_stack.jj.client import JjClient
from jj_stack.models.stack import LocalRevision

from .models import SubmitResult, SubmittedRevision


def print_submit_result(result: SubmitResult) -> None:
    """Print the final submit result."""

    client = result.client
    # Overlap the `jj log` subprocess startup cost before we print the final summary for large
    # stacks.
    with console.spinner(description="Rendering jj log"):
        prerendered_blocks = render_revision_blocks(
            client=client,
            revisions=tuple(revision.prepared.revision for revision in result.revisions)
            + (result.trunk,),
        )
    if not result.revisions:
        for line in _render_submit_trunk_lines(
            client=client,
            prerendered_lines=prerendered_blocks.get(result.trunk.commit_id),
            trunk=result.trunk,
        ):
            console.output(line, soft_wrap=True)
        console.note(
            "The selected stack has no changes to review.",
            soft_wrap=True,
        )
        return

    if result.dry_run:
        console.note("Dry run: no local, remote, or GitHub changes applied.", soft_wrap=True)
        console.output("Planned changes:")
    else:
        console.output("Submitted changes:")
    for revision in reversed(result.revisions):
        for line in _render_submit_revision_lines(
            client=client,
            prerendered_lines=prerendered_blocks.get(revision.prepared.revision.commit_id),
            revision=revision,
        ):
            console.output(line, soft_wrap=True)
    for line in _render_submit_trunk_lines(
        client=client,
        prerendered_lines=prerendered_blocks.get(result.trunk.commit_id),
        trunk=result.trunk,
    ):
        console.output(line, soft_wrap=True)
    if not result.dry_run:
        top_pull_request_url = result.revisions[-1].pull_request_url
        if top_pull_request_url is not None:
            console.output(ui.prefixed_line("Top of stack: ", top_pull_request_url))


def render_selected_line(
    *,
    selected_change_id: str,
    selected_subject: str,
) -> ui.PrefixedLine:
    """Render the selected stack head line."""

    return ui.prefixed_line(
        "Selected: ",
        t"{selected_subject} ({ui.change_id(selected_change_id)})",
    )


def _render_submit_revision_lines(
    *,
    client: JjClient,
    prerendered_lines: tuple[str, ...] | None = None,
    revision: SubmittedRevision,
) -> tuple[ui.Renderable, ...]:
    parts: list[str] = []
    if revision.pull_request_action != "created":
        if revision.prepared.remote_action == "up to date":
            parts.append("already pushed")
        else:
            parts.append("pushed")

    if revision.pull_request_number is None:
        if revision.pull_request_action == "created":
            parts.append("new PR")
        elif revision.pull_request_action == "updated":
            parts.append("PR updated")
        else:
            parts.append("PR unchanged")
    else:
        label = format_pull_request_label(
            revision.pull_request_number,
            is_draft=bool(revision.pull_request_is_draft),
        )
        if revision.pull_request_action == "created":
            parts.append(label)
        else:
            parts.append(f"{label} {revision.pull_request_action}")

    summary = ", ".join(parts)
    return render_revision_lines(
        client=client,
        prerendered_lines=prerendered_lines,
        revision=revision.prepared.revision,
        suffix=summary,
    )


def _render_submit_trunk_lines(
    *,
    client: JjClient,
    prerendered_lines: tuple[str, ...] | None = None,
    trunk: LocalRevision,
) -> tuple[ui.Renderable, ...]:
    return render_revision_lines(
        client=client,
        prerendered_lines=prerendered_lines,
        revision=trunk,
    )
