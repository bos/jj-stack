"""Compare the local and GitHub state of the selected `jj` stacks.

Show submitted and unsubmitted changes with their current PR state. Long stacks are summarized;
use `--verbose` to show every change.

PR state comes from GitHub and stack order comes from local history. This command does not
fetch. Run `jj git fetch` first if you need to update local `trunk()`. Pass several revsets or
repeat `--pull-request` to inspect several stacks in one run.

Common examples:

- `jj-stack view` inspects the stack ending at `@` when the working-copy change is described and
  nonempty, otherwise `@-`.

- `jj-stack view --pull-request 123` finds the full local stack containing that PR.

- `jj-stack view <change-id>` finds the full local stack containing that change.

In terminals with hyperlink support, click a PR label to open it on GitHub. The PR in the
"Submitted stack" heading links to the topmost submitted PR.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.text import Text

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._json_status import stack_change_json
from jj_stack.errors import EXIT_INCOMPLETE, CliError, UnsupportedStackError, error_message
from jj_stack.formatting import (
    CommitRenderClient,
    RenderableCommit,
    format_pr_label,
    render_commit_blocks,
    render_commit_lines,
)
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import (
    JjCommandError,
    divergent_change_id_from_error,
)
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.preparation import PreparedLocalStack, prepare_local_stack
from jj_stack.stack.reporting import report_change, status_label, submittable_edits
from jj_stack.stack.selected import is_change_id_prefix
from jj_stack.stack.selection import resolve_linked_change_for_pr
from jj_stack.stack.status import (
    StackStatusChange,
    StatusResult,
    build_status_result,
    observe_status,
)

_SUMMARY_SECTION_HEAD_COUNT = 3
_SUMMARY_SECTION_TAIL_COUNT = 3

HELP = "Check the PR status of one or more jj stacks"

ViewSelectorKind = Literal["pr", "revset"]


@dataclass(frozen=True, slots=True)
class ViewSelector:
    """One explicit selector from the `view` command line."""

    kind: ViewSelectorKind
    value: str


def view(
    *,
    as_json: bool,
    cli_args: JjCliArgs,
    debug: bool,
    repo: Path | None,
    selectors: tuple[ViewSelector, ...],
    verbose: bool,
) -> int:
    """CLI entrypoint for `view`."""

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    return _run_status(
        context=context,
        selectors=selectors,
        as_json=as_json,
        verbose=verbose,
    )


def _run_status(
    *,
    as_json: bool,
    context: CommandContext,
    selectors: tuple[ViewSelector, ...],
    verbose: bool,
) -> int:
    selections: Sequence[
        tuple[ViewSelector | None, PreparedLocalStack | CliError, tuple[ui.Message, ...]]
    ]
    if selectors:
        selections = _prepare_status_selections(context=context, selectors=selectors)
    else:
        selections = ((None, _prepare_status_with_spinner(context=context, revset=None), ()),)
    with console.spinner(description="Inspecting GitHub"):
        pr_lookups = observe_status(
            context=context,
            prepared=tuple(
                prepared
                for _, prepared, _ in selections
                if isinstance(prepared, PreparedLocalStack)
            ),
        )
    exit_code = 0
    multi_selector = len(selectors) > 1
    json_stacks: list[dict[str, object]] = []
    for index, (selector, prepared_status, notes) in enumerate(selections):
        if not as_json:
            if index:
                console.output("")
            if multi_selector and selector is not None:
                console.output(_status_heading(selector))
        if isinstance(prepared_status, CliError):
            console.warning(ui.prefixed_line("Error: ", error_message(prepared_status)))
            hint = prepared_status.hint
            if hint is not None:
                console.warning(ui.prefixed_line("Hint: ", hint))
            exit_code = EXIT_INCOMPLETE
            continue

        for warning in _local_history_warnings(prepared_status):
            console.warning(warning)
        result = build_status_result(prepared=prepared_status, pr_lookups=pr_lookups)
        exit_code = max(exit_code, EXIT_INCOMPLETE if result.incomplete else 0)
        if as_json:
            _warn_about_unavailable_github(result)
            json_stacks.append(
                _json_status_result(
                    prepared_status=prepared_status,
                    result=result,
                    selector=selector,
                )
            )
            continue

        for note in notes:
            console.note(note)
        _render_prepared_status(
            prepared_status=prepared_status,
            result=result,
            verbose=verbose,
        )
    if as_json:
        console.machine_output(json.dumps({"stacks": json_stacks}, indent=2))
    return exit_code


def _prepare_status_selections(
    *,
    context: CommandContext,
    selectors: tuple[ViewSelector, ...],
) -> list[tuple[ViewSelector, PreparedLocalStack | CliError, tuple[ui.Message, ...]]]:
    selections: list[
        tuple[ViewSelector, PreparedLocalStack | CliError, tuple[ui.Message, ...]]
    ] = []
    stack_keys: set[tuple[str, ...]] = set()
    for selector in selectors:
        try:
            prepared, notes = _prepare_status_selector(context=context, selector=selector)
        except CliError as error:
            if len(selectors) == 1:
                # Without a report, preserve the selection error's category.
                raise
            selections.append((selector, error, ()))
            continue
        stack_key = (
            prepared.stack.base_parent.commit_id,
            *(change.change_id for change in prepared.stack.changes),
        )
        if stack_key not in stack_keys:
            stack_keys.add(stack_key)
            selections.append((selector, prepared, notes))
    return selections


def _prepare_status_selector(
    *,
    context: CommandContext,
    selector: ViewSelector,
) -> tuple[PreparedLocalStack, tuple[ui.Message, ...]]:
    if selector.kind == "pr":
        resolved_revset, note = resolve_linked_change_for_pr(
            jj_client=context.jj_client,
            pr_reference=selector.value,
            revset=None,
        )
        prepared = _prepare_status_with_spinner(
            context=context,
            containing_change_id=resolved_revset,
            revset=None,
        )
        return prepared, (note,)
    resolved_revset = selector.value
    containing_change_id = _change_id_selector(
        context=context,
        value=resolved_revset,
    )
    return (
        _prepare_status_with_spinner(
            context=context,
            revset=None if containing_change_id is not None else resolved_revset,
            containing_change_id=containing_change_id,
        ),
        (),
    )


def _change_id_selector(*, context: CommandContext, value: str) -> str | None:
    """Recognize a bare change ID without misclassifying a bookmark."""

    if not is_change_id_prefix(value):
        return None
    try:
        change = context.jj_client.resolve_commit(value)
    except JjCommandError as error:
        if divergent_change_id_from_error(error) == value:
            return value
        raise
    except CliError as error:
        # A change-ID selector that matched nothing is a selection that does not form a
        # supported local stack, which is what the commit-ID form already reports. Only the
        # base class means that: every subclass `resolve_commit` can raise is a different
        # answer that keeps its own exit code and hint, including a stale workspace, an
        # ambiguous prefix and an unparseable revset. No `reason`: this cannot tell a hidden
        # change from a change ID that never existed.
        if type(error) is not CliError:
            raise
        raise UnsupportedStackError(error.message) from error
    return value if change.change_id.startswith(value) else None


def _prepare_status_with_spinner(
    *,
    containing_change_id: str | None = None,
    context: CommandContext,
    revset: str | None,
) -> PreparedLocalStack:
    with console.spinner(description="Inspecting jj stack"):
        return prepare_local_stack(
            context=context,
            containing_change_id=containing_change_id,
            fetch_remote_state=False,
            inspection_mode=True,
            revset=revset,
        )


def _local_history_warnings(prepared_status: PreparedLocalStack) -> tuple[ui.Message, ...]:
    """Describe local states that inspection tolerates but stack mutation rejects."""

    warnings: list[ui.Message] = []
    for change in prepared_status.stack.changes:
        change_id = ui.change_id(change.change_id)
        if len(change.parents) > 1:
            warnings.append(
                t"Change {change_id} has multiple parents. Showing its first-parent path; "
                t"submitting requires a linear stack."
            )
        if change.empty:
            warnings.append(t"Change {change_id} is empty and cannot be submitted.")
        elif not change.description.strip():
            warnings.append(
                t"Change {change_id} has no description. Before submitting, describe it with "
                t"{ui.cmd(f'jj describe {short_change_id(change.change_id)}')}."
            )
        if change.conflict:
            warnings.append(
                t"Change {change_id} has unresolved conflicts. Resolve them before running "
                t"{ui.cmd('jj-stack submit')} or {ui.cmd('jj-stack merge')}."
            )
    return tuple(warnings)


def _status_heading(selector: ViewSelector) -> ui.Message:
    if selector.kind == "pr":
        return f"Status for PR {selector.value}:"
    return t"Status for {ui.revset(selector.value)}:"


def _warn_about_unavailable_github(result: StatusResult) -> tuple[ui.Message, ...]:
    """Explain an unreachable remote or GitHub target on stderr in every output mode.

    A `--json` caller reads a well-formed payload on stdout, so the reason for a non-zero
    exit code has nowhere to go but stderr.
    """

    lines = remote_and_github_unavailable_messages(
        github_error=result.github_error,
        github_repo=result.github_repo,
        remote=result.remote,
        remote_error=result.remote_error,
    )
    _emit_lines(lines, emitter=console.warning, soft_wrap=False)
    return lines


def _json_status_result(
    *,
    prepared_status: PreparedLocalStack,
    result: StatusResult,
    selector: ViewSelector | None,
) -> dict[str, object]:
    stack_model = prepared_status.stack
    current_change_ids = {
        change.change_id for change in stack_model.changes if change.current_working_copy
    }
    stack: dict[str, object] = {
        "head_change_id": stack_model.head.change_id,
        "changes": [
            stack_change_json(
                change,
                current=change.change_id in current_change_ids,
            )
            for change in result.changes
        ],
    }
    if selector is not None:
        stack["selector"] = f"PR {selector.value}" if selector.kind == "pr" else selector.value
    return stack


def _render_prepared_status(
    *,
    prepared_status: PreparedLocalStack,
    result: StatusResult,
    verbose: bool,
) -> None:
    warning_lines = _warn_about_unavailable_github(result)

    if not prepared_status.stack.changes:
        _emit_lines(
            render_empty_status_lines(
                prepared_status=prepared_status,
            )
        )
        return

    with console.spinner(description="Rendering jj log"):
        prerendered_blocks = _prefetch_commit_log_blocks(
            client=prepared_status.client,
            changes=result.changes,
            trunk=prepared_status.stack.base_parent,
        )
    _emit_lines(
        render_status_summary_lines(
            result=result,
            leading_separator=bool(warning_lines),
            verbose=verbose,
            prerendered_blocks=prerendered_blocks,
        )
    )
    _emit_lines(
        render_trunk_status_lines(
            prerendered_blocks[prepared_status.stack.base_parent.commit_id],
        )
    )
    _emit_lines(
        render_status_advisory_lines(
            result=result,
        )
    )


def render_status_summary_lines(
    *,
    leading_separator: bool,
    result,
    verbose: bool,
    prerendered_blocks: dict[str, tuple[str, ...]],
) -> tuple[ui.Renderable, ...]:
    """Render capped submitted and unsubmitted summaries before the trunk row."""

    unsubmitted_changes = tuple(change for change in result.changes if change.tracked is None)
    submitted_changes = tuple(change for change in result.changes if change.tracked is not None)

    lines: list[ui.Renderable] = []
    unsubmitted_lines = _render_summary_section(
        "Unsubmitted stack",
        include_leading_separator=leading_separator,
        changes=unsubmitted_changes,
        verbose=verbose,
        renderer=lambda change: _render_summary_change_lines(
            change=change,
            repo=result.github_repo,
            show_status=False,
            prerendered_blocks=prerendered_blocks,
        ),
    )
    if unsubmitted_lines:
        lines.extend(unsubmitted_lines)

    submitted_lines = _render_summary_section(
        _render_submitted_section_title(submitted_changes),
        include_leading_separator=False,
        changes=submitted_changes,
        verbose=verbose,
        renderer=lambda change: _render_summary_change_lines(
            change=change,
            repo=result.github_repo,
            show_status=True,
            prerendered_blocks=prerendered_blocks,
        ),
    )
    if submitted_lines:
        if lines:
            lines.append("")
        lines.extend(submitted_lines)
    return tuple(lines)


def render_trunk_status_lines(
    raw_lines: tuple[str, ...],
) -> tuple[ui.Renderable, ...]:
    """Render the trunk footer with the user's `jj log` formatting."""

    if len(raw_lines) > 1 and Text.from_ansi(raw_lines[-1]).plain.strip() in {"|", "│", "┃"}:
        return raw_lines[:-1]
    return raw_lines


def render_empty_status_lines(
    *,
    prepared_status: PreparedLocalStack,
) -> tuple[ui.Renderable, ...]:
    """Render the empty-stack footer and explanation."""

    trunk = prepared_status.stack.base_parent
    blocks = render_commit_blocks(client=prepared_status.client, changes=(trunk,))
    return (
        *render_trunk_status_lines(
            blocks[trunk.commit_id],
        ),
        "The selected stack has no changes to show.",
    )


def _prefetch_commit_log_blocks(
    *,
    client: CommitRenderClient,
    changes: tuple[StackStatusChange, ...],
    trunk: RenderableCommit,
) -> dict[str, tuple[str, ...]]:
    """Render the `jj log` block for every change we will print, in parallel."""

    seen: set[str] = set()
    ordered: list[RenderableCommit] = []
    for change in (*changes, trunk):
        if change.commit_id in seen:
            continue
        seen.add(change.commit_id)
        ordered.append(change)
    return render_commit_blocks(client=client, changes=tuple(ordered))


def _render_summary_section(
    title: ui.Message,
    *,
    include_leading_separator: bool,
    changes: tuple,
    renderer,
    verbose: bool,
) -> tuple[ui.Renderable, ...]:
    """Render one capped summary section."""

    if not changes and not verbose:
        return ()

    heading: ui.Message = f"{title}:" if isinstance(title, str) else t"{title}:"
    lines: list[ui.Renderable] = [heading]
    if include_leading_separator:
        lines.insert(0, "")
    if not changes:
        lines.append("  (none)")
        return tuple(lines)

    rendered = [renderer(change) for change in changes]
    if verbose or len(rendered) <= _SUMMARY_SECTION_HEAD_COUNT + _SUMMARY_SECTION_TAIL_COUNT + 1:
        for block in rendered:
            lines.extend(block)
        return tuple(lines)

    omitted = len(rendered) - _SUMMARY_SECTION_HEAD_COUNT - _SUMMARY_SECTION_TAIL_COUNT
    for block in rendered[:_SUMMARY_SECTION_HEAD_COUNT]:
        lines.extend(block)
    lines.append(f"   ... {omitted} changes omitted ...")
    for block in rendered[-_SUMMARY_SECTION_TAIL_COUNT:]:
        lines.extend(block)
    return tuple(lines)


def _render_submitted_section_title(changes: tuple) -> ui.Message:
    """Render the submitted-section heading, linking the newest submitted PR when possible."""

    top_pr = changes[0].pr if changes else None
    if top_pr is None:
        return "Submitted stack"
    label = format_pr_label(top_pr.number, url=top_pr.html_url)
    return t"Submitted stack ({label})"


def render_status_advisory_lines(
    *,
    result: StatusResult,
) -> tuple[ui.Renderable, ...]:
    """Render any advisories that follow the status stack output."""

    reports = {change.change_id: report_change(change.state) for change in result.changes}
    cleanup_changes = [
        change for change in result.changes if reports[change.change_id].needs_sync
    ]
    divergent_changes = [
        change for change in result.changes if reports[change.change_id].divergent
    ]
    repair_changes = [
        change for change in result.changes if reports[change.change_id].repair is not None
    ]
    closed_changes = [
        change
        for change in result.changes
        if reports[change.change_id].lifecycle == "closed"
        and reports[change.change_id].repair is None
    ]
    submitted_disagreements = tuple(reversed(submittable_edits(reports)))
    if (
        not cleanup_changes
        and not divergent_changes
        and not repair_changes
        and not closed_changes
        and not submitted_disagreements
    ):
        return ()

    rows: list[tuple[ui.TableCell, ui.TableCell]] = []
    if submitted_disagreements:
        rows.append(
            (
                "Submit needed",
                "Local changes have not been submitted",
            )
        )
        rows.append(
            (
                "Meaning",
                "jj-stack submit will update the PR branches and bases to match local history",
            )
        )
        if cleanup_changes:
            rows.append(
                (
                    "After syncing",
                    (
                        ui.cmd("jj-stack submit"),
                        " ",
                        ui.revset(result.selected_revset),
                    ),
                )
            )
        else:
            rows.append(
                (
                    "Next step",
                    (
                        ui.cmd("jj-stack submit"),
                        " ",
                        ui.revset(result.selected_revset),
                    ),
                )
            )
        if len(submitted_disagreements) == 1:
            disagreement_detail: ui.Message = ui.change_id(submitted_disagreements[0])
        else:
            visible_change_ids = tuple(submitted_disagreements[:5])
            remaining = len(submitted_disagreements) - len(visible_change_ids)
            disagreement_detail = (
                f"{len(submitted_disagreements)} changes: ",
                *ui.join(ui.change_id, visible_change_ids),
                *((", ", f"... {remaining} more") if remaining else ()),
            )
        rows.append(("Changed locally", disagreement_detail))

    if cleanup_changes:
        rows.append(
            (
                "Sync needed",
                "Merged changes remain in local history. Run jj-stack sync to update the "
                "stack and any remaining PRs",
            )
        )
        rows.append(
            (
                "Preview first",
                (
                    ui.cmd("jj-stack sync --dry-run"),
                    " ",
                    ui.revset(result.selected_revset),
                ),
            )
        )
        rows.append(
            (
                "Apply",
                (
                    ui.cmd("jj-stack sync"),
                    " ",
                    ui.revset(result.selected_revset),
                ),
            )
        )
        for change in cleanup_changes:
            pr = change.pr
            pr_label: ui.Message = (
                format_pr_label(pr.number, url=pr.html_url) if pr is not None else "merged PR"
            )
            rows.append(
                (
                    ui.change_id(change.change_id),
                    (
                        pr_label,
                        " is merged; the local stack still includes this change",
                    ),
                )
            )

    if closed_changes:
        rows.append(
            (
                "Closed GitHub PR" if len(closed_changes) == 1 else "Closed GitHub PRs",
                (
                    "Reopen the PR on GitHub to continue using it, link an open replacement "
                    "with jj-stack relink, or clean up with ",
                    ui.cmd(f"jj-stack cleanup {result.selected_revset}"),
                    " before submitting again.",
                ),
            )
        )
    for change in repair_changes:
        report = reports[change.change_id]
        rows.append(
            (
                ui.change_id(change.change_id),
                (
                    status_label(report.status),
                    ": ",
                    report.reason or "",
                    "; ",
                    report.repair or "",
                ),
            )
        )
    if any(report.problem == "branch_moved" for report in reports.values()):
        rows.append(
            (
                "GitHub stack rebase",
                (
                    "If GitHub rewrote the stack, apply its result with ",
                    ui.cmd(f"jj-stack sync {result.selected_revset}"),
                    " (preview with ",
                    ui.option("--dry-run"),
                    ").",
                ),
            )
        )

    for change in divergent_changes:
        rows.append(
            (
                ui.change_id(change.change_id),
                divergence_recovery_hint(
                    change.change_id,
                    retry="retry the jj-stack command",
                ),
            )
        )
    return (
        "",
        "Advisories:",
        ui.DataTable(
            columns=(
                ui.TableColumn("advisory", no_wrap=True),
                ui.TableColumn("detail"),
            ),
            rows=tuple(rows),
            box="none",
            padding=(0, 2),
            show_header=False,
        ),
    )


def _render_summary_change_lines(
    *,
    change: StackStatusChange,
    repo: GithubRepoAddress | None,
    show_status: bool,
    prerendered_blocks: dict[str, tuple[str, ...]],
) -> tuple[ui.Renderable, ...]:
    """Render one change inside a submitted or unsubmitted summary section."""

    summary = _format_status_summary(change, repo=repo)
    if not show_status and summary == "not submitted":
        summary = None
    return render_commit_lines(
        prerendered_blocks[change.commit_id],
        suffix=summary,
    )


def _format_status_summary(
    change: StackStatusChange,
    *,
    repo: GithubRepoAddress | None,
) -> ui.Message:
    report = report_change(change.state)
    pr = change.pr
    if pr is not None:
        pr_label = format_pr_label(
            pr.number, is_draft=pr.state == "open" and pr.is_draft, url=pr.html_url
        )
        if report.needs_sync:
            summary: ui.Message = t"{pr_label} merged, sync needed"
        elif report.lifecycle in {"open", "draft"}:
            summary = pr_label
        elif report.lifecycle == "merged":
            summary = t"{pr_label} merged"
        else:
            summary = t"{pr_label} {status_label(report.lifecycle)}"
        if report.checks is not None:
            summary = t"{summary}, checks {report.checks}"
    elif change.tracked is not None:
        summary = format_pr_label(
            change.tracked.pr_identity.pr_number, prefix="saved ", repo=repo
        )
    else:
        summary = "not submitted"
    if report.problem is not None:
        summary = t"{summary}, {status_label(report.problem)}"
    if report.divergent:
        summary = t"{summary}, {status_label('divergent')}"
    return summary


def _emit_lines(
    lines: tuple[ui.Renderable, ...], *, emitter=console.output, soft_wrap: bool = True
) -> None:
    for line in lines:
        emitter(line, soft_wrap=soft_wrap)
