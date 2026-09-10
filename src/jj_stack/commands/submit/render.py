"""Render submit previews and completed publication effects."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.formatting import format_pr_label, render_commit_blocks, render_commit_lines
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit

from .github_stack import GithubStackPlan
from .models import PRSyncPlan, PublicationInputs


def print_submit_preview(
    *,
    inputs: PublicationInputs,
    plans: tuple[PRSyncPlan, ...],
    github_stack_plan: GithubStackPlan,
) -> None:
    actions = (
        [
            f"would dissolve GitHub stack #{stack.number}"
            for stack in github_stack_plan.affected_stacks
        ]
        if github_stack_plan.action == "replace"
        else []
    )
    if github_stack_plan.action == "append":
        actions.append(
            f"would extend GitHub stack #{github_stack_plan.affected_stacks[0].number}"
        )
    elif github_stack_plan.creates_stack(len(plans)):
        actions.append(f"would create a GitHub stack with {len(plans)} PRs")
    print_submit_rows(
        inputs=inputs,
        rows=tuple((plan.prepared.change, _preview_summary(plan)) for plan in plans),
        heading="Dry run: planned changes:",
    )
    if actions:
        summary = ", ".join(actions)
        console.output(f"{summary[0].upper()}{summary[1:]}.")


def print_submitted_changes(
    *,
    inputs: PublicationInputs,
    changes: tuple[tuple[PRSyncPlan, GithubPR], ...],
) -> None:
    rows: list[tuple[LocalCommit, ui.Message]] = []
    for plan, pr in changes:
        parts: list[ui.Message] = []
        if plan.action != "created":
            parts.append(
                "already pushed" if plan.prepared.remote_action == "up to date" else "pushed"
            )
        label = format_pr_label(pr.number, is_draft=pr.is_draft, url=pr.html_url)
        parts.append(label if plan.action == "created" else t"{label} {plan.action}")
        rows.append((plan.prepared.change, ui.join(lambda part: part, parts)))
    print_submit_rows(inputs=inputs, rows=tuple(rows), heading="Submitted changes:")
    if changes:
        _, top_pr = changes[-1]
        console.output(
            ui.prefixed_line(
                "Top of stack: ", format_pr_label(top_pr.number, url=top_pr.html_url)
            )
        )


def print_submit_rows(
    *,
    inputs: PublicationInputs,
    rows: tuple[tuple[LocalCommit, ui.Message], ...],
    heading: str,
) -> None:
    """Render a submit's change rows and trunk using the user's jj log format."""

    trunk = inputs.stack.trunk
    with console.spinner(description="Rendering jj log"):
        blocks = render_commit_blocks(
            client=inputs.client,
            changes=tuple(change for change, _ in rows) + (trunk,),
        )
    if rows:
        console.output(heading)
    for change, summary in reversed(rows):
        for line in render_commit_lines(blocks[change.commit_id], suffix=summary):
            console.output(line, soft_wrap=True)
    for line in render_commit_lines(blocks[trunk.commit_id]):
        console.output(line, soft_wrap=True)
    if not rows:
        console.note("The selected stack has no changes to submit.", soft_wrap=True)


def print_selected_line(selected_change_id: str, selected_subject: str) -> None:
    """Print the selected stack head line."""

    console.output(
        ui.prefixed_line(
            "Selected: ",
            t"{selected_subject} ({ui.change_id(selected_change_id)})",
        )
    )


def _preview_summary(plan: PRSyncPlan) -> ui.Message:
    prepared = plan.prepared
    parts: list[ui.Message] = [
        t"push {ui.bookmark(prepared.branch)}"
        if prepared.remote_action == "pushed"
        else "branch up to date"
    ]
    if prepared.pr is None:
        kind = "draft PR" if plan.draft else "PR"
        parts.append(t"create {kind} against {ui.bookmark(plan.base_branch)}")
        if plan.generated_description.title != prepared.change.subject:
            parts.append(t"title: {plan.generated_description.title}")
    else:
        parts.append(format_pr_label(prepared.pr.number, url=prepared.pr.html_url))
        base, body, title = plan.content_updates
        if base is not None:
            parts.append(t"base: {ui.bookmark(prepared.pr.base.ref)} → {ui.bookmark(base)}")
        if title is not None:
            parts.append(t"title: {title}")
        if body is not None:
            parts.append("update body")
        if plan.draft_action is not None:
            parts.append(
                "convert to draft" if plan.draft_action == "draft" else "mark ready for review"
            )
    if plan.metadata is not None:
        for verb, values in (
            ("add labels", plan.metadata.labels),
            ("request reviewers", plan.metadata.reviewers),
            ("request team reviewers", plan.metadata.team_reviewers),
        ):
            if values:
                parts.append(t"{verb}: {', '.join(values)}")
    return ui.join(lambda part: part, parts)
