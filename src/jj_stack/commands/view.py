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
from jj_stack.commands._json_status import review_change_json
from jj_stack.errors import EXIT_INCOMPLETE, CliError, error_message
from jj_stack.formatting import (
    RenderableRevision,
    RevisionRenderClient,
    format_pull_request_label,
    render_revision_blocks,
    render_revision_lines,
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
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
)
from jj_stack.review.selected import is_change_id_prefix
from jj_stack.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_stack.review.status import (
    PreparedStack,
    PreparedStatus,
    PullRequestLookup,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    status_preparation_cli_error,
    stream_status,
)
from jj_stack.review_namespace import ReviewNamespace

_SUMMARY_SECTION_HEAD_COUNT = 3
_SUMMARY_SECTION_TAIL_COUNT = 3

HELP = "Check the review status of one or more `jj` stacks"

ViewSelectorKind = Literal["pull_request", "revset"]


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
class _ClassifiedStatusRevision:
    """Rendered status revision paired with its derived review status."""

    revision: ReviewStatusRevision
    status: ReviewChangeStatus


def view(
    *,
    as_json: bool,
    cli_args: JjCliArgs,
    debug: bool,
    pull_request: str | Sequence[str] | None,
    repository: Path | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[ViewSelector] | None = None,
    verbose: bool,
) -> int:
    """CLI entrypoint for `view`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    return _run_status(
        context=context,
        selectors=_normalize_status_selectors(
            pull_request=pull_request,
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
            namespace=context.review_namespace,
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
            revision.revision.change_id for revision in prepared_status.prepared.status_revisions
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
                namespace=context.review_namespace,
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
    pull_request: str | Sequence[str] | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[ViewSelector] | None,
) -> tuple[ViewSelector, ...]:
    if selectors is not None:
        return tuple(selectors)

    ordered: list[ViewSelector] = []
    if pull_request is not None:
        if isinstance(pull_request, str):
            ordered.append(ViewSelector(kind="pull_request", value=pull_request))
        else:
            ordered.extend(
                ViewSelector(kind="pull_request", value=value) for value in pull_request
            )
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
    if selector.kind == "pull_request":
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            jj_client=context.jj_client,
            pull_request_reference=selector.value,
            revset=None,
        )
        return _ResolvedViewSelector(
            note=t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}",
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
        revision = context.jj_client.resolve_revision(value)
    except JjCommandError as error:
        if divergent_change_id_from_error(error) == value:
            return value
        raise
    return value if revision.change_id.startswith(value) else None


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
    """Describe selected local states that inspection tolerates but review mutation rejects."""

    warnings: list[ui.Message] = []
    for revision in prepared_status.prepared.stack.revisions:
        change_id = ui.change_id(revision.change_id)
        if len(revision.parents) > 1:
            warnings.append(
                t"Change {change_id} is a merge commit. Showing its first-parent path; "
                t"commands that change review state require a linear stack."
            )
        if revision.is_working_copy and revision.empty:
            warnings.append(
                t"Change {change_id} is an empty working-copy commit. Showing it for inspection; "
                t"it cannot be submitted for review."
            )
        elif revision.is_working_copy and not revision.description.strip():
            warnings.append(
                t"Change {change_id} has no description. Showing it for inspection, but it "
                t"cannot be submitted until it is described with {ui.cmd('jj describe')}."
            )
        if revision.divergent:
            warnings.append(
                t"Change {change_id} has divergent local commits. Showing the selected path; "
                t"commands that change review state will stop until the divergence is resolved."
            )
        if revision.conflict:
            warnings.append(
                t"Change {change_id} has unresolved conflicts. Showing it for inspection; "
                t"submit and merge remain blocked until the conflicts are resolved."
            )
    return tuple(warnings)


def _status_heading(selector: ViewSelector) -> ui.Message:
    if selector.kind == "pull_request":
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
            on_revision=lambda _revision, _github_available: progress.advance(),
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
        revision.change_id for revision in stack_model.revisions if revision.current_working_copy
    }
    stack: dict[str, object] = {
        "changes": [
            review_change_json(
                revision,
                current=revision.change_id in current_change_ids,
            )
            for revision in result.revisions
        ],
    }
    if selector is not None:
        stack["selector"] = (
            f"PR {selector.value}" if selector.kind == "pull_request" else selector.value
        )
    return stack


def _render_prepared_status(
    *,
    namespace: ReviewNamespace,
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
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )

    github_message = github_unavailable_message(
        github_error=result.github_error,
        github_repository=result.github_repository,
    )
    github_lines = () if github_message is None else (github_message,)
    if result.github_error is not None:
        _emit_lines(github_lines, emitter=console.warning, soft_wrap=False)
    else:
        _emit_lines(github_lines)

    if not prepared_status.prepared.status_revisions:
        _emit_lines(
            render_empty_status_lines(
                prepared_status=prepared_status,
            )
        )
        return 0

    github_available = result.github_repository is not None and result.github_error is None
    with console.spinner(description="Rendering jj log"):
        prerendered_blocks = _prefetch_revision_log_blocks(
            client=prepared_status.prepared.client,
            revisions=result.revisions,
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
            namespace=namespace,
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

    classified_revisions = tuple(
        _ClassifiedStatusRevision(
            revision=revision,
            status=classify_review_status_revision(revision),
        )
        for revision in result.revisions
    )
    unsubmitted_revisions = tuple(
        classified
        for classified in classified_revisions
        if _classify_revision_for_summary(classified) == "unsubmitted"
    )
    submitted_revisions = tuple(
        classified
        for classified in classified_revisions
        if _classify_revision_for_summary(classified) == "submitted"
    )

    lines: list[str] = []
    unsubmitted_lines = _render_summary_section(
        "Unsubmitted stack",
        include_leading_separator=leading_separator,
        revisions=unsubmitted_revisions,
        verbose=verbose,
        renderer=lambda classified: _render_summary_revision_lines(
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
            tuple(classified.revision for classified in submitted_revisions)
        ),
        include_leading_separator=False,
        revisions=submitted_revisions,
        verbose=verbose,
        renderer=lambda classified: _render_summary_revision_lines(
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
    return render_revision_lines(
        client=prepared.client,
        revision=trunk,
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
        "The selected stack has no changes to review.",
    )


def _prefetch_revision_log_blocks(
    *,
    client: RevisionRenderClient,
    revisions: tuple[ReviewStatusRevision, ...],
    trunk: RenderableRevision,
) -> dict[str, tuple[str, ...]]:
    """Render the `jj log` block for every revision we will print, in parallel."""

    seen: set[str] = set()
    ordered: list[RenderableRevision] = []
    for revision in (*revisions, trunk):
        if revision.commit_id in seen:
            continue
        seen.add(revision.commit_id)
        ordered.append(revision)
    return render_revision_blocks(client=client, revisions=tuple(ordered))


def _render_summary_section(
    title: str,
    *,
    include_leading_separator: bool,
    revisions: tuple,
    renderer,
    verbose: bool,
) -> tuple[str, ...]:
    """Render one capped summary section."""

    if not revisions and not verbose:
        return ()

    lines = [f"{title}:"]
    if include_leading_separator:
        lines.insert(0, "")
    if not revisions:
        lines.append("  (none)")
        return tuple(lines)

    rendered = [renderer(revision) for revision in revisions]
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


def _render_submitted_section_title(revisions: tuple) -> str:
    """Render the submitted-section heading, linking the newest submitted PR when possible."""

    if revisions:
        _lookup = revisions[0].pull_request_lookup
        top_pull_request_url = (
            _lookup.pull_request.html_url
            if _lookup is not None and _lookup.pull_request is not None
            else None
        )
    else:
        top_pull_request_url = None
    if top_pull_request_url is None:
        return "Submitted stack"
    return f"Submitted stack ({top_pull_request_url})"


def render_status_advisory_lines(
    *,
    namespace: ReviewNamespace,
    result: StatusResult,
) -> tuple[ui.Renderable, ...]:
    """Render any advisories that follow the status stack output."""

    classified_revisions = tuple(
        _ClassifiedStatusRevision(
            revision=revision,
            status=classify_review_status_revision(revision),
        )
        for revision in result.revisions
    )
    cleanup_revisions = [
        classified
        for classified in classified_revisions
        if classified.status.pr_lifecycle == "merged"
    ]
    divergent_revisions = [
        classified
        for classified in classified_revisions
        if classified.status.local == "divergent" and classified.status.pr_lifecycle != "merged"
    ]
    link_revisions = [
        classified
        for classified in classified_revisions
        if _classified_revision_has_link_advisory(classified)
    ]
    submitted_disagreements = result.submitted_state_disagreements
    policy_warning_rows: list[tuple[ui.TableCell, ui.TableCell]] = []
    for classified in cleanup_revisions:
        revision = classified.revision
        lookup = revision.pull_request_lookup
        pull_request = lookup.pull_request if lookup is not None else None
        if pull_request is None:
            continue
        base_ref = pull_request.base.ref
        if not namespace.contains(base_ref):
            continue
        policy_warning_rows.append(
            (
                "Repository policy",
                t"Repository policy warning: PR #{pull_request.number} merged into "
                t"{ui.bookmark(base_ref)}; configure GitHub to block merges of PRs "
                t"targeting {ui.bookmark(namespace.branch_glob)}",
            )
        )
    if (
        not cleanup_revisions
        and not divergent_revisions
        and not link_revisions
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
        if cleanup_revisions:
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

    if cleanup_revisions:
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
        for revision in cleanup_revisions:
            pull_request_number = revision.revision.pull_request_number()
            pull_request_label = (
                f"PR #{pull_request_number}" if pull_request_number is not None else "merged PR"
            )
            rows.append(
                (
                    ui.change_id(revision.revision.change_id),
                    (
                        pull_request_label,
                        " is merged, and later local changes are still based on it",
                    ),
                )
            )

    if link_revisions:
        rows.append(
            _link_advisory_summary_row(
                link_revisions=tuple(link_revisions),
                selected_revset=result.selected_revset,
            )
        )
        for revision in link_revisions:
            rows.append(
                (
                    ui.change_id(revision.revision.change_id),
                    _describe_link_advisory(revision),
                )
            )

    rows.extend(policy_warning_rows)

    for revision in divergent_revisions:
        rows.append(
            (
                ui.change_id(revision.revision.change_id),
                t"Resolve the multiple visible revisions for this change before retrying "
                t"({ui.cmd('jj log -r')} "
                t"{ui.revset(f'change_id({revision.revision.change_id})')})",
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
    link_revisions: tuple[_ClassifiedStatusRevision, ...],
    selected_revset: str,
) -> tuple[ui.TableCell, ui.TableCell]:
    states = {_link_advisory_kind(revision) for revision in link_revisions}
    change_phrase = (
        "the change shown above"
        if len(link_revisions) == 1
        else "one or more changes shown above"
    )
    cleanup_command = ui.cmd(f"jj-stack cleanup {selected_revset}")
    if states == {"closed"}:
        label = "Closed GitHub PR" if len(link_revisions) == 1 else "Closed GitHub PRs"
        closed_phrase = "a closed PR" if len(link_revisions) == 1 else "closed PRs"
        detail = (
            f"GitHub reports {closed_phrase} for {change_phrase}; submit will not "
            "reuse closed reviews. Reopen the PR on GitHub to continue that review, "
            "relink an open replacement, or remove the closed review's leftovers with ",
            cleanup_command,
            " before submitting again.",
        )
        return label, detail
    if states == {"missing"}:
        label = "Missing GitHub PR" if len(link_revisions) == 1 else "Missing GitHub PRs"
        detail = (
            "GitHub did not report a PR for the remembered review branch of "
            f"{change_phrase}. Run ",
            ui.cmd("jj git fetch"),
            " if branch state may be stale. Relink an open PR if one exists; otherwise forget "
            "the missing PR link with ",
            ui.cmd(f"jj-stack unstack --local {selected_revset}"),
            " before submitting again.",
        )
        return label, detail
    if states == {"ambiguous"}:
        label = "Ambiguous GitHub PR" if len(link_revisions) == 1 else "Ambiguous GitHub PRs"
        detail = (
            "GitHub reports multiple PRs for the remembered review branch of "
            f"{change_phrase}. Run ",
            ui.cmd("jj git fetch"),
            " to refresh, then relink the intended open PR.",
        )
        return label, detail
    if states == {"remembered"}:
        label = "Saved GitHub PR" if len(link_revisions) == 1 else "Saved GitHub PRs"
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


def _link_advisory_kind(classified: _ClassifiedStatusRevision) -> str:
    revision = classified.revision
    lookup = revision.pull_request_lookup
    if lookup is None:
        raise AssertionError("Link advisory requires a pull request lookup.")
    change_status = classified.status
    if lookup.source == "remembered" and lookup.message is not None:
        return "remembered"
    if change_status.pr_lifecycle in {"ambiguous", "closed", "missing"}:
        return change_status.pr_lifecycle
    raise AssertionError(f"Unexpected link advisory state: {change_status.pr_lifecycle}")


def _render_summary_revision_lines(
    *,
    classified: _ClassifiedStatusRevision,
    client,
    github_available: bool,
    show_status: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render one revision inside a submitted or unsubmitted summary section."""

    revision = classified.revision
    summary = _format_status_summary(classified, github_available=github_available)
    if not show_status and summary == "not submitted":
        summary = None
    return render_revision_lines(
        client=client,
        revision=revision,
        suffix=summary,
        prerendered_lines=(
            prerendered_blocks.get(revision.commit_id) if prerendered_blocks else None
        ),
    )


def _classify_revision_for_summary(
    classified: _ClassifiedStatusRevision,
) -> str:
    """Classify a revision into submitted, unsubmitted, or other."""

    change_status = classified.status
    if change_status.pr_lifecycle in {"open", "closed", "merged"}:
        return "submitted"
    if change_status.saved_review_identity:
        return "submitted"
    return "unsubmitted"


def _format_status_summary(
    classified: _ClassifiedStatusRevision,
    *,
    github_available: bool,
) -> str:
    revision = classified.revision
    lookup = revision.pull_request_lookup
    review_identity = revision.review_identity
    saved_label = _format_saved_pull_request_label(review_identity)
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
        if lookup.pull_request is None:
            raise AssertionError("Open pull request lookup must include a pull request.")
        summary = _format_live_pull_request_label(
            lookup=lookup,
            pull_request_number=lookup.pull_request.number,
            is_draft=lookup.pull_request.is_draft,
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
        if lookup.pull_request is None:
            raise AssertionError("Closed pull request lookup must include a pull request.")
        pr_label = _format_live_pull_request_label(
            lookup=lookup,
            pull_request_number=lookup.pull_request.number,
            is_draft=False,
        )
        if change_status.pr_lifecycle == "merged":
            summary = f"{pr_label} merged into {lookup.pull_request.base.ref}, cleanup needed"
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
        summary = f"{summary}, multiple visible revisions"

    return summary


def _format_live_pull_request_label(
    *,
    lookup: PullRequestLookup,
    pull_request_number: int,
    is_draft: bool,
) -> str:
    prefix = "remembered " if lookup.source == "remembered" else ""
    return format_pull_request_label(
        pull_request_number,
        is_draft=is_draft,
        prefix=prefix,
    )


def _emit_lines(
    lines: tuple[ui.Renderable, ...], *, emitter=console.output, soft_wrap: bool = True
) -> None:
    for line in lines:
        emitter(line, soft_wrap=soft_wrap)


def _format_saved_pull_request_label(review_identity: ReviewIdentity | None) -> str | None:
    if review_identity is None:
        return None
    # Identity-only tracking has no lifecycle to show; --fetch reports it live.
    return format_pull_request_label(review_identity.pr_number, prefix="saved ")


def _classified_revision_has_link_advisory(
    classified: _ClassifiedStatusRevision,
) -> bool:
    change_status = classified.status
    revision = classified.revision
    lookup = revision.pull_request_lookup
    if lookup is None:
        return False
    if lookup.source == "remembered" and lookup.message is not None:
        return True
    if change_status.pr_lifecycle == "ambiguous":
        return True
    if change_status.pr_lifecycle == "missing":
        return change_status.has_stale_pull_request_link
    if change_status.pr_lifecycle == "closed":
        return lookup.pull_request is not None
    return False


def _describe_link_advisory(classified: _ClassifiedStatusRevision) -> ui.Message:
    revision = classified.revision
    lookup = revision.pull_request_lookup
    if lookup is None:
        raise AssertionError("Link advisory requires a pull request lookup.")
    change_status = classified.status
    if lookup.source == "remembered" and lookup.message is not None:
        return lookup.message
    if change_status.pr_lifecycle == "ambiguous":
        return lookup.message or "GitHub reports more than one matching pull request"
    if change_status.pr_lifecycle == "missing":
        review_identity = revision.review_identity
        if review_identity is not None:
            return (
                f"GitHub did not report remembered PR #{review_identity.pr_number} "
                "for this branch"
            )
        saved_label = _format_saved_pull_request_label(review_identity)
        if saved_label is None:
            return "GitHub did not report a pull request for this branch"
        return f"GitHub did not report {saved_label} for this branch"
    if change_status.pr_lifecycle == "closed":
        pull_request = lookup.pull_request
        if pull_request is None:
            raise AssertionError("Closed pull request advisory requires a pull request.")
        return (
            f"PR #{pull_request.number} is {pull_request.state}; submit will not reuse a "
            "closed review automatically"
        )
    raise AssertionError(f"Unexpected link advisory state: {change_status.pr_lifecycle}")
