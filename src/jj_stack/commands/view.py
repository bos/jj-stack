"""Compare the local and GitHub state of the selected `jj` stacks.

By default it summarizes the submitted and unsubmitted changes in each selected stack;
`--verbose` expands those summaries.

It reads pull request state from GitHub, but derives stack membership from the local DAG using
local `trunk()` as the lower boundary. If your local copy of trunk is behind, `jj-stack`'s picture
of membership can be stale even though the pull request state is current. Run `jj git fetch`
first when the view needs to reflect the latest trunk. Mix revsets and repeated `--pull-request`
values to inspect several stacks in one run.

Common examples:

- `jj-stack view` inspects the stack ending at `@` when the working-copy change is described and
  nonempty, otherwise `@-`.

- `jj-stack view --pull-request 123` finds the full local stack containing that PR.

- `jj-stack view <change-id>` finds the full local stack containing that change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._json_status import stack_change_json
from jj_stack.errors import EXIT_INCOMPLETE, CliError, error_message
from jj_stack.formatting import (
    CommitRenderClient,
    RenderableCommit,
    format_pr_label,
    render_commit_blocks,
    render_commit_lines,
)
from jj_stack.github.error_messages import (
    github_unavailable_message,
    remote_unavailable_message,
)
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import (
    JjCommandError,
    UnsupportedStackError,
    divergent_change_id_from_error,
)
from jj_stack.models.tracking import PRIdentity
from jj_stack.pr_branch_namespace import current_pr_branch_namespace
from jj_stack.stack.change_status import (
    ChangeStatus,
    classify_stack_status_change,
)
from jj_stack.stack.selected import is_change_id_prefix
from jj_stack.stack.selection import (
    resolve_linked_change_for_pr,
    resolve_selected_revset,
)
from jj_stack.stack.status import (
    PreparedStack,
    PreparedStatus,
    PRLookup,
    StackStatusChange,
    StatusResult,
    prepare_status,
    status_preparation_cli_error,
    stream_status,
)

_SUMMARY_SECTION_HEAD_COUNT = 3
_SUMMARY_SECTION_TAIL_COUNT = 3

HELP = "Check the PR status of one or more `jj` stacks"

ViewSelectorKind = Literal["pr", "revset"]


@dataclass(frozen=True, slots=True)
class ViewSelector:
    """One explicit selector from the `view` command line."""

    kind: ViewSelectorKind
    value: str


@dataclass(frozen=True, slots=True)
class _ResolvedViewSelector:
    note: ui.Message | None
    revset: str | None
    containing_change_id: str | None


@dataclass(frozen=True, slots=True)
class _ClassifiedStatusChange:
    """Rendered status change paired with its derived PR status."""

    change: StackStatusChange
    status: ChangeStatus


def view(
    *,
    as_json: bool,
    cli_args: JjCliArgs,
    debug: bool,
    pr: str | Sequence[str] | None,
    repo: Path | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[ViewSelector] | None = None,
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
        selectors=_normalize_status_selectors(
            pr=pr,
            revset=revset,
            selectors=selectors,
        ),
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
    if not selectors:
        prepared_status = _prepare_status_with_spinner(
            context=context,
            revset=None,
        )
        if as_json:
            rendered, incomplete = _json_prepared_status(
                prepared_status=prepared_status,
            )
            console.machine_output(json.dumps(_view_json_payload(stacks=(rendered,)), indent=2))
            return EXIT_INCOMPLETE if incomplete else 0
        exit_code = _render_prepared_status(
            prepared_status=prepared_status,
            verbose=verbose,
        )
        return exit_code

    exit_code = 0
    multi_selector = len(selectors) > 1
    rendered_stack_keys: set[tuple[str, ...]] = set()
    json_stacks: list[dict[str, object]] = []
    printed_blocks = 0
    for selector in selectors:
        try:
            resolved_selector = _resolve_status_selector(
                context=context,
                selector=selector,
            )
            prepared_status = _prepare_status_with_spinner(
                containing_change_id=resolved_selector.containing_change_id,
                context=context,
                revset=resolved_selector.revset,
            )
        except CliError as error:
            if not multi_selector:
                # A single selector that yields no report matches the bare-view
                # behavior: fail with the error's category code instead of
                # degrading to an incomplete report.
                raise
            if not as_json and printed_blocks:
                console.output("")
            if not as_json:
                console.output(_status_heading(selector))
            console.warning(ui.prefixed_line("Error: ", error_message(error)))
            hint = error.hint
            if hint is not None:
                console.warning(ui.prefixed_line("Hint: ", hint))
            exit_code = EXIT_INCOMPLETE
            if not as_json:
                printed_blocks += 1
            continue

        change_ids = tuple(
            change.change.change_id for change in prepared_status.prepared.status_changes
        )
        stack_key = (prepared_status.prepared.stack.base_parent.commit_id, *change_ids)
        if stack_key in rendered_stack_keys:
            continue
        rendered_stack_keys.add(stack_key)
        if as_json:
            rendered, incomplete = _json_prepared_status(
                prepared_status=prepared_status,
                selector=selector,
            )
            json_stacks.append(rendered)
            exit_code = max(exit_code, EXIT_INCOMPLETE if incomplete else 0)
            continue

        if printed_blocks:
            console.output("")
        if multi_selector:
            console.output(_status_heading(selector))
        if resolved_selector.note is not None:
            console.note(resolved_selector.note)
        exit_code = max(
            exit_code,
            _render_prepared_status(
                prepared_status=prepared_status,
                verbose=verbose,
            ),
        )
        printed_blocks += 1
    if as_json:
        console.machine_output(
            json.dumps(
                _view_json_payload(
                    stacks=tuple(json_stacks),
                ),
                indent=2,
            )
        )
        return exit_code
    return exit_code


def _normalize_status_selectors(
    *,
    pr: str | Sequence[str] | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[ViewSelector] | None,
) -> tuple[ViewSelector, ...]:
    if selectors is not None:
        return tuple(selectors)

    ordered: list[ViewSelector] = []
    if pr is not None:
        if isinstance(pr, str):
            ordered.append(ViewSelector(kind="pr", value=pr))
        else:
            ordered.extend(ViewSelector(kind="pr", value=value) for value in pr)
    if revset is not None:
        if isinstance(revset, str):
            ordered.append(ViewSelector(kind="revset", value=revset))
        else:
            ordered.extend(ViewSelector(kind="revset", value=value) for value in revset)
    return tuple(ordered)


def _resolve_status_selector(
    *,
    context: CommandContext,
    selector: ViewSelector,
) -> _ResolvedViewSelector:
    if selector.kind == "pr":
        pr_number, resolved_revset = resolve_linked_change_for_pr(
            jj_client=context.jj_client,
            pr_reference=selector.value,
            revset=None,
        )
        return _ResolvedViewSelector(
            note=t"Using PR #{pr_number} -> {ui.revset(resolved_revset)}",
            revset=None,
            containing_change_id=resolved_revset,
        )
    resolved_revset = resolve_selected_revset(
        command_label="view",
        default_revset=None,
        require_explicit=False,
        revset=selector.value,
    )
    containing_change_id = _change_id_selector(
        context=context,
        value=resolved_revset,
    )
    return _ResolvedViewSelector(
        note=None,
        revset=None if containing_change_id is not None else resolved_revset,
        containing_change_id=containing_change_id,
    )


def _change_id_selector(*, context: CommandContext, value: str | None) -> str | None:
    """Recognize a bare change ID without misclassifying a bookmark."""

    if not is_change_id_prefix(value):
        return None
    assert value is not None
    try:
        change = context.jj_client.resolve_commit(value)
    except JjCommandError as error:
        if divergent_change_id_from_error(error) == value:
            return value
        raise
    return value if change.change_id.startswith(value) else None


def _prepare_status_for_revset(
    *,
    containing_change_id: str | None = None,
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus:
    try:
        prepared_status = prepare_status(
            context=context,
            containing_change_id=containing_change_id,
            fetch_remote_state=False,
            inspection_mode=True,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    return prepared_status


def _prepare_status_with_spinner(
    *,
    containing_change_id: str | None = None,
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus:
    with console.spinner(description="Inspecting jj stack"):
        prepared_status = _prepare_status_for_revset(
            containing_change_id=containing_change_id,
            context=context,
            revset=revset,
        )
    for warning in _local_history_warnings(prepared_status):
        console.warning(warning)
    return prepared_status


def _local_history_warnings(prepared_status: PreparedStatus) -> tuple[ui.Message, ...]:
    """Describe local states that inspection tolerates but stack mutation rejects."""

    warnings: list[ui.Message] = []
    for change in prepared_status.prepared.stack.changes:
        change_id = ui.change_id(change.change_id)
        if len(change.parents) > 1:
            warnings.append(
                t"Change {change_id} is a merge change. Showing its first-parent path; "
                t"commands that change stack state require a linear stack."
            )
        if change.is_working_copy and change.empty:
            warnings.append(
                t"Change {change_id} is an empty working-copy change. Showing it for inspection; "
                t"it cannot be submitted."
            )
        elif change.is_working_copy and not change.description.strip():
            warnings.append(
                t"Change {change_id} has no description. Showing it for inspection, but it "
                t"cannot be submitted until it is described with {ui.cmd('jj describe')}."
            )
        if change.divergent:
            warnings.append(
                t"Change {change_id} has divergent local commits. Showing the selected path; "
                t"commands that change stack state will stop until the divergence is resolved."
            )
        if change.conflict:
            warnings.append(
                t"Change {change_id} has unresolved conflicts. Showing it for inspection; "
                t"submit and merge remain blocked until the conflicts are resolved."
            )
    return tuple(warnings)


def _status_heading(selector: ViewSelector) -> ui.Message:
    if selector.kind == "pr":
        return f"Status for PR {selector.value}:"
    return t"Status for {ui.revset(selector.value)}:"


def _json_prepared_status(
    *,
    prepared_status: PreparedStatus,
    selector: ViewSelector | None = None,
) -> tuple[dict[str, object], bool]:
    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        result = stream_status(
            on_change=lambda _change, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    return (
        _json_status_result(
            prepared_status=prepared_status,
            result=result,
            selector=selector,
        ),
        result.incomplete,
    )


def _view_json_payload(
    *,
    stacks: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return {
        "stacks": list(stacks),
    }


def _json_status_result(
    *,
    prepared_status: PreparedStatus,
    result: StatusResult,
    selector: ViewSelector | None,
) -> dict[str, object]:
    stack_model = prepared_status.prepared.stack
    current_change_ids = {
        change.change_id for change in stack_model.changes if change.current_working_copy
    }
    stack: dict[str, object] = {
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
    prepared_status: PreparedStatus,
    verbose: bool,
) -> int:
    selection_lines = (
        ()
        if prepared_status.prepared.remote is not None
        else (remote_unavailable_message(remote_error=prepared_status.prepared.remote_error),)
    )
    if selection_lines:
        _emit_lines(selection_lines, emitter=console.warning)

    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        result = stream_status(
            on_change=lambda _change, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )

    github_message = github_unavailable_message(
        github_error=result.github_error,
        github_repo=result.github_repo,
    )
    github_lines = () if github_message is None else (github_message,)
    if result.github_error is not None:
        _emit_lines(github_lines, emitter=console.warning, soft_wrap=False)
    else:
        _emit_lines(github_lines)

    if not prepared_status.prepared.status_changes:
        _emit_lines(
            render_empty_status_lines(
                prepared_status=prepared_status,
            )
        )
        return 0

    github_available = result.github_repo is not None and result.github_error is None
    with console.spinner(description="Rendering jj log"):
        prerendered_blocks = _prefetch_commit_log_blocks(
            client=prepared_status.prepared.client,
            changes=result.changes,
            trunk=prepared_status.prepared.stack.base_parent,
        )
    _emit_lines(
        render_status_summary_lines(
            client=prepared_status.prepared.client,
            result=result,
            github_available=github_available,
            leading_separator=bool(selection_lines or github_lines),
            verbose=verbose,
            prerendered_blocks=prerendered_blocks,
        )
    )
    _emit_lines(
        render_trunk_status_lines(
            prepared=prepared_status.prepared,
            prerendered_blocks=prerendered_blocks,
        )
    )
    _emit_lines(
        render_status_advisory_lines(
            result=result,
        )
    )

    return EXIT_INCOMPLETE if result.incomplete else 0


def render_status_summary_lines(
    *,
    client,
    github_available: bool,
    leading_separator: bool,
    result,
    verbose: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render capped submitted and unsubmitted summaries before the trunk row."""

    classified_changes = tuple(
        _ClassifiedStatusChange(
            change=change,
            status=classify_stack_status_change(change),
        )
        for change in result.changes
    )
    unsubmitted_changes = tuple(
        classified
        for classified in classified_changes
        if _classify_change_for_summary(classified) == "unsubmitted"
    )
    submitted_changes = tuple(
        classified
        for classified in classified_changes
        if _classify_change_for_summary(classified) == "submitted"
    )

    lines: list[str] = []
    unsubmitted_lines = _render_summary_section(
        "Unsubmitted stack",
        include_leading_separator=leading_separator,
        changes=unsubmitted_changes,
        verbose=verbose,
        renderer=lambda classified: _render_summary_change_lines(
            classified=classified,
            client=client,
            github_available=github_available,
            show_status=False,
            prerendered_blocks=prerendered_blocks,
        ),
    )
    if unsubmitted_lines:
        lines.extend(unsubmitted_lines)

    submitted_lines = _render_summary_section(
        _render_submitted_section_title(
            tuple(classified.change for classified in submitted_changes)
        ),
        include_leading_separator=False,
        changes=submitted_changes,
        verbose=verbose,
        renderer=lambda classified: _render_summary_change_lines(
            classified=classified,
            client=client,
            github_available=github_available,
            show_status=True,
            prerendered_blocks=prerendered_blocks,
        ),
    )
    if submitted_lines:
        if lines:
            lines.append("")
        lines.extend(submitted_lines)
    if lines:
        lines.append("")
    return tuple(lines)


def render_trunk_status_lines(
    *,
    prepared: PreparedStack,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render the trunk footer with the user's `jj log` formatting."""

    trunk = prepared.stack.base_parent
    return render_commit_lines(
        client=prepared.client,
        change=trunk,
        prerendered_lines=(
            prerendered_blocks.get(trunk.commit_id) if prerendered_blocks else None
        ),
    )


def render_empty_status_lines(
    *,
    prepared_status: PreparedStatus,
) -> tuple[ui.Message, ...]:
    """Render the empty-stack footer and explanation."""

    return (
        *render_trunk_status_lines(
            prepared=prepared_status.prepared,
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
    title: str,
    *,
    include_leading_separator: bool,
    changes: tuple,
    renderer,
    verbose: bool,
) -> tuple[str, ...]:
    """Render one capped summary section."""

    if not changes and not verbose:
        return ()

    lines = [f"{title}:"]
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


def _render_submitted_section_title(changes: tuple) -> str:
    """Render the submitted-section heading, linking the newest submitted PR when possible."""

    if changes:
        _lookup = changes[0].pr_lookup
        top_pr_url = (
            _lookup.pr.html_url if _lookup is not None and _lookup.pr is not None else None
        )
    else:
        top_pr_url = None
    if top_pr_url is None:
        return "Submitted stack"
    return f"Submitted stack ({top_pr_url})"


def render_status_advisory_lines(
    *,
    result: StatusResult,
) -> tuple[ui.Renderable, ...]:
    """Render any advisories that follow the status stack output."""

    namespace = current_pr_branch_namespace()
    classified_changes = tuple(
        _ClassifiedStatusChange(
            change=change,
            status=classify_stack_status_change(change),
        )
        for change in result.changes
    )
    cleanup_changes = [
        classified
        for classified in classified_changes
        if classified.status.pr_lifecycle == "merged"
    ]
    divergent_changes = [
        classified
        for classified in classified_changes
        if classified.status.local == "divergent" and classified.status.pr_lifecycle != "merged"
    ]
    link_changes = [
        classified
        for classified in classified_changes
        if _classified_change_has_link_advisory(classified)
    ]
    submitted_disagreements = result.submitted_state_disagreements
    policy_warning_rows: list[tuple[ui.TableCell, ui.TableCell]] = []
    for classified in cleanup_changes:
        change = classified.change
        lookup = change.pr_lookup
        pr = lookup.pr if lookup is not None else None
        if pr is None:
            continue
        base_ref = pr.base.ref
        if not namespace.contains(base_ref):
            continue
        policy_warning_rows.append(
            (
                "Repo policy",
                t"Repo policy warning: PR #{pr.number} merged into "
                t"{ui.bookmark(base_ref)}; configure GitHub to block merges of PRs "
                t"targeting {ui.bookmark(namespace.branch_glob)}",
            )
        )
    if (
        not cleanup_changes
        and not divergent_changes
        and not link_changes
        and not submitted_disagreements
        and not policy_warning_rows
    ):
        return ()

    rows: list[tuple[ui.TableCell, ui.TableCell]] = []
    if submitted_disagreements:
        rows.append(
            (
                "Submit needed",
                "PR branches are behind the current local stack",
            )
        )
        rows.append(
            (
                "Meaning",
                "Submit will push the current commit IDs and PR bases to GitHub",
            )
        )
        if cleanup_changes:
            rows.append(
                (
                    "After cleanup",
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
        rows.extend(_submitted_state_disagreement_rows(submitted_disagreements))

    if cleanup_changes:
        rows.append(
            (
                "Sync needed",
                "Submit note: descendant PR bases still follow the old local ancestry "
                "until the remaining selected changes are synced",
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
                "If the plan is safe",
                (
                    ui.cmd("jj-stack sync"),
                    " ",
                    ui.revset(result.selected_revset),
                ),
            )
        )
        for change in cleanup_changes:
            pr_number = change.change.pr_number()
            pr_label = f"PR #{pr_number}" if pr_number is not None else "merged PR"
            rows.append(
                (
                    ui.change_id(change.change.change_id),
                    (
                        pr_label,
                        " is merged, and later local changes are still based on it",
                    ),
                )
            )

    if link_changes:
        rows.append(
            _link_advisory_summary_row(
                link_changes=tuple(link_changes),
                selected_revset=result.selected_revset,
            )
        )
        for change in link_changes:
            rows.append(
                (
                    ui.change_id(change.change.change_id),
                    _describe_link_advisory(change),
                )
            )

    rows.extend(policy_warning_rows)

    for change in divergent_changes:
        rows.append(
            (
                ui.change_id(change.change.change_id),
                t"Resolve the multiple visible commits for this change before retrying "
                t"({ui.cmd('jj log -r')} "
                t"{ui.revset(f'change_id({change.change.change_id})')})",
            )
        )
    return ("", "Advisories:", _advisory_table(tuple(rows)))


def _submitted_state_disagreement_rows(
    disagreements: Sequence[str],
) -> tuple[tuple[ui.TableCell, ui.TableCell], ...]:
    if not disagreements:
        return ()
    return (
        (
            "New commit IDs",
            _format_submit_baseline_reason(
                change_ids=tuple(disagreements),
                noun="change",
            ),
        ),
    )


def _format_submit_baseline_reason(
    *,
    change_ids: Sequence[str],
    noun: str,
) -> ui.Message:
    if len(change_ids) == 1:
        return ui.change_id(change_ids[0])
    plural_noun = f"{noun}s" if len(change_ids) != 1 else noun
    return (f"{len(change_ids)} {plural_noun}: ", *_format_change_id_list(change_ids))


def _format_change_id_list(
    change_ids: Sequence[str], *, limit: int = 5
) -> tuple[ui.Message, ...]:
    visible = tuple(change_ids[:limit])
    rendered = list(ui.join(ui.change_id, visible))
    remaining = len(change_ids) - limit
    if remaining > 0:
        if rendered:
            rendered.append(", ")
        rendered.append(f"... {remaining} more")
    return tuple(rendered)


def _advisory_table(rows: tuple[tuple[ui.TableCell, ui.TableCell], ...]) -> ui.DataTable:
    return ui.DataTable(
        columns=(
            ui.TableColumn("advisory", no_wrap=True),
            ui.TableColumn("detail"),
        ),
        rows=rows,
        box="none",
        padding=(0, 2),
        show_header=False,
    )


def _link_advisory_summary_row(
    *,
    link_changes: tuple[_ClassifiedStatusChange, ...],
    selected_revset: str,
) -> tuple[ui.TableCell, ui.TableCell]:
    states = {_link_advisory_kind(change) for change in link_changes}
    change_phrase = (
        "the change shown above" if len(link_changes) == 1 else "one or more changes shown above"
    )
    cleanup_command = ui.cmd(f"jj-stack cleanup {selected_revset}")
    if states == {"closed"}:
        label = "Closed GitHub PR" if len(link_changes) == 1 else "Closed GitHub PRs"
        closed_phrase = "a closed PR" if len(link_changes) == 1 else "closed PRs"
        detail = (
            f"GitHub reports {closed_phrase} for {change_phrase}; submit will not "
            "reuse closed pull requests. Reopen the PR on GitHub to continue using it, "
            "relink an open replacement, or remove the closed PR's leftovers with ",
            cleanup_command,
            " before submitting again.",
        )
        return label, detail
    if states == {"missing"}:
        label = "Missing GitHub PR" if len(link_changes) == 1 else "Missing GitHub PRs"
        detail = (
            f"GitHub did not report a PR for the remembered PR branch of {change_phrase}. Run ",
            ui.cmd("jj git fetch"),
            " if branch state may be stale. Relink an open PR if one exists; otherwise forget "
            "the missing PR link with ",
            ui.cmd(f"jj-stack unstack --local {selected_revset}"),
            " before submitting again.",
        )
        return label, detail
    if states == {"ambiguous"}:
        label = "Ambiguous GitHub PR" if len(link_changes) == 1 else "Ambiguous GitHub PRs"
        detail = (
            f"GitHub reports multiple PRs for the remembered PR branch of {change_phrase}. Run ",
            ui.cmd("jj git fetch"),
            " to refresh, then relink the intended open PR.",
        )
        return label, detail
    if states == {"remembered"}:
        label = "Saved GitHub PR" if len(link_changes) == 1 else "Saved GitHub PRs"
        detail = (
            "GitHub found the remembered PR, but its head branch no longer matches "
            f"{change_phrase}. Relink it if that PR should stay attached; "
            "otherwise forget the incorrect link with ",
            ui.cmd(f"jj-stack unstack --local {selected_revset}"),
            " before submitting again.",
        )
        return label, detail
    detail = (
        "GitHub reports closed, missing, or ambiguous PR state for one or more "
        "changes shown above. Inspect the per-change rows, then reopen, relink, clean up, or "
        "forget a saved link as appropriate.",
    )
    return "GitHub PRs need repair", detail


def _link_advisory_kind(classified: _ClassifiedStatusChange) -> str:
    change = classified.change
    lookup = change.pr_lookup
    if lookup is None:
        raise AssertionError("Link advisory requires a pull request lookup.")
    change_status = classified.status
    if lookup.source == "remembered" and lookup.message is not None:
        return "remembered"
    if change_status.pr_lifecycle in {"ambiguous", "closed", "missing"}:
        return change_status.pr_lifecycle
    raise AssertionError(f"Unexpected link advisory state: {change_status.pr_lifecycle}")


def _render_summary_change_lines(
    *,
    classified: _ClassifiedStatusChange,
    client,
    github_available: bool,
    show_status: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render one change inside a submitted or unsubmitted summary section."""

    change = classified.change
    summary = _format_status_summary(classified, github_available=github_available)
    if not show_status and summary == "not submitted":
        summary = None
    return render_commit_lines(
        client=client,
        change=change,
        suffix=summary,
        prerendered_lines=(
            prerendered_blocks.get(change.commit_id) if prerendered_blocks else None
        ),
    )


def _classify_change_for_summary(
    classified: _ClassifiedStatusChange,
) -> str:
    """Classify a change into submitted, unsubmitted, or other."""

    change_status = classified.status
    if change_status.pr_lifecycle in {"open", "closed", "merged"}:
        return "submitted"
    if change_status.saved_pr_identity:
        return "submitted"
    return "unsubmitted"


def _format_status_summary(
    classified: _ClassifiedStatusChange,
    *,
    github_available: bool,
) -> str:
    change = classified.change
    lookup = change.pr_lookup
    pr_identity = change.pr_identity
    saved_label = _format_saved_pr_label(pr_identity)
    change_status = classified.status
    summary: str
    if change_status.pr_lifecycle == "none" and not change_status.pr_lookup_error:
        if saved_label is not None:
            summary = saved_label
        elif github_available:
            summary = "not submitted"
        else:
            summary = "GitHub status unknown"
    elif change_status.pr_lifecycle == "open":
        if lookup is None:
            raise AssertionError("Open pull request status requires a pull request lookup.")
        if lookup.pr is None:
            raise AssertionError("Open pull request lookup must include a pull request.")
        summary = _format_live_pr_label(
            lookup=lookup,
            pr_number=lookup.pr.number,
            is_draft=lookup.pr.is_draft,
        )
        review_decision = change_status.pr_review_decision
        if review_decision == "unknown" and lookup.review_decision_error is not None:
            review_decision = "none"
        if change_status.pr_queued is True:
            summary = f"{summary} queued"
        elif change_status.pr_draft is True:
            pass
        elif review_decision == "approved":
            summary = f"{summary} approved"
        elif review_decision == "changes_requested":
            summary = f"{summary} changes requested"
    elif change_status.pr_lifecycle == "missing":
        if saved_label is not None:
            summary = f"{saved_label}, no PR found for branch"
        else:
            summary = "not submitted"
    elif change_status.pr_lifecycle in {"closed", "merged"}:
        if lookup is None:
            raise AssertionError("Closed pull request status requires a pull request lookup.")
        if lookup.pr is None:
            raise AssertionError("Closed pull request lookup must include a pull request.")
        pr_label = _format_live_pr_label(
            lookup=lookup,
            pr_number=lookup.pr.number,
            is_draft=False,
        )
        if change_status.pr_lifecycle == "merged":
            summary = f"{pr_label} merged into {lookup.pr.base.ref}, cleanup needed"
        else:
            summary = f"{pr_label} closed"
    else:
        message = (
            ui.plain_text(lookup.message)
            if lookup is not None and lookup.message is not None
            else "GitHub lookup failed"
        )
        if saved_label is not None:
            summary = f"{saved_label}, {message}"
        else:
            summary = message

    if change_status.local == "divergent" and change_status.pr_lifecycle != "merged":
        summary = f"{summary}, multiple visible commits"

    return summary


def _format_live_pr_label(
    *,
    lookup: PRLookup,
    pr_number: int,
    is_draft: bool,
) -> str:
    prefix = "remembered " if lookup.source == "remembered" else ""
    return format_pr_label(
        pr_number,
        is_draft=is_draft,
        prefix=prefix,
    )


def _emit_lines(
    lines: tuple[ui.Renderable, ...], *, emitter=console.output, soft_wrap: bool = True
) -> None:
    for line in lines:
        emitter(line, soft_wrap=soft_wrap)


def _format_saved_pr_label(pr_identity: PRIdentity | None) -> str | None:
    if pr_identity is None:
        return None
    # Identity-only tracking has no lifecycle to show; --fetch reports it live.
    return format_pr_label(pr_identity.pr_number, prefix="saved ")


def _classified_change_has_link_advisory(
    classified: _ClassifiedStatusChange,
) -> bool:
    change_status = classified.status
    change = classified.change
    lookup = change.pr_lookup
    if lookup is None:
        return False
    if lookup.source == "remembered" and lookup.message is not None:
        return True
    if change_status.pr_lifecycle == "ambiguous":
        return True
    if change_status.pr_lifecycle == "missing":
        return change_status.has_stale_pr_link
    if change_status.pr_lifecycle == "closed":
        return lookup.pr is not None
    return False


def _describe_link_advisory(classified: _ClassifiedStatusChange) -> ui.Message:
    change = classified.change
    lookup = change.pr_lookup
    if lookup is None:
        raise AssertionError("Link advisory requires a pull request lookup.")
    change_status = classified.status
    if lookup.source == "remembered" and lookup.message is not None:
        return lookup.message
    if change_status.pr_lifecycle == "ambiguous":
        return lookup.message or "GitHub reports more than one matching pull request"
    if change_status.pr_lifecycle == "missing":
        pr_identity = change.pr_identity
        if pr_identity is not None:
            return f"GitHub did not report remembered PR #{pr_identity.pr_number} for this branch"
        saved_label = _format_saved_pr_label(pr_identity)
        if saved_label is None:
            return "GitHub did not report a pull request for this branch"
        return f"GitHub did not report {saved_label} for this branch"
    if change_status.pr_lifecycle == "closed":
        pr = lookup.pr
        if pr is None:
            raise AssertionError("Closed pull request advisory requires a pull request.")
        return (
            f"PR #{pr.number} is {pr.state}; submit will not reuse a "
            "closed pull request automatically"
        )
    raise AssertionError(f"Unexpected link advisory state: {change_status.pr_lifecycle}")
