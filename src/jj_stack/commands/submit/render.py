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

from .models import SubmitResult, SubmittedRevision


def print_submit_result(result: SubmitResult) -> None:
    """Print the final submit result."""

    client = result.client
    prerendered_blocks: dict[str, tuple[str, ...]] = {}
    if client is not None:
        # Overlap the native `jj log` subprocess startup cost before we print
        # the final summary for large stacks.
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
            result=result,
        ):
            _print_submit_line(line, client=client)
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
            _print_submit_line(line, client=client)
    for line in _render_submit_trunk_lines(
        client=client,
        prerendered_lines=prerendered_blocks.get(result.trunk.commit_id),
        result=result,
    ):
        _print_submit_line(line, client=client)
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


def _print_submit_line(line: ui.Renderable, *, client: JjClient | None) -> None:
    if client is None:
        console.output(line)
    else:
        console.output(line, soft_wrap=True)


def _render_submit_revision_lines(
    *,
    client: JjClient | None,
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
    if client is None:
        return (
            ui.prefixed_line(
                "- ",
                t"{revision.prepared.revision.subject} "
                t"({ui.change_id(revision.change_id)}): {summary}",
            ),
        )
    return render_revision_lines(
        client=client,
        prerendered_lines=prerendered_lines,
        revision=revision.prepared.revision,
        bookmark=revision.prepared.bookmark,
        suffix=summary,
    )


def _render_submit_trunk_lines(
    *,
    client: JjClient | None,
    prerendered_lines: tuple[str, ...] | None = None,
    result: SubmitResult,
) -> tuple[ui.Renderable, ...]:
    if client is None:
        return (
            ui.prefixed_line(
                "Trunk: ",
                t"{result.trunk_subject} ({ui.change_id(result.trunk_change_id)}) "
                t"-> {ui.bookmark(result.trunk_branch)}",
            ),
        )
    return render_revision_lines(
        client=client,
        prerendered_lines=prerendered_lines,
        revision=result.trunk,
    )
