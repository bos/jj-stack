"""Show how the selected jj stack(s) currently appear locally and on GitHub.

By default it summarizes the submitted and unsubmitted changes in each selected stack;
`--verbose` expands those summaries and includes any bookmark names.

`--fetch` runs a fetch first so the report uses current remote branch locations. Use one or more
revsets and `--pull-request` selectors to inspect several stacks in one run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.config import RepoConfig
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.formatting import (
    format_pull_request_label,
    format_status_annotation,
    render_revision_blocks,
    render_revision_lines,
    short_change_id,
)
from jj_review.github.error_messages import (
    github_unavailable_message,
    remote_unavailable_message,
)
from jj_review.jj import JjCliArgs, JjClient, UnsupportedStackError
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalStack
from jj_review.review.bookmarks import bookmark_glob, is_review_bookmark
from jj_review.review.change_status import (
    SubmittedStateDisagreement,
    classify_review_status_revision,
    classify_saved_review_change,
    submitted_state_disagreement,
)
from jj_review.review.discovery import discover_connected_tracked_stacks
from jj_review.review.operations import (
    OrderedOperationMatch,
    describe_operation,
    match_cleanup_rebase_operation,
    match_close_operation,
)
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_review.review.status import (
    StatusResult,
    prepare_status,
    refresh_remote_state_for_status,
    status_preparation_cli_error,
    stream_status,
)
from jj_review.review.submit_recovery import (
    SubmitRecoveryIdentity,
    SubmitStatusDecision,
    submit_status_decision,
)
from jj_review.state.journal import (
    CleanupOperationRecord,
    CleanupRebaseOperationRecord,
    CloseOperationRecord,
    LandOperationRecord,
    RelinkOperationRecord,
    SubmitOperationRecord,
)
from jj_review.system import pid_is_alive

_SUMMARY_SECTION_HEAD_COUNT = 3
_SUMMARY_SECTION_TAIL_COUNT = 3

HELP = "Check the review status of one or more jj stacks"

StatusSelectorKind = Literal["pull_request", "revset"]


@dataclass(frozen=True, slots=True)
class StatusSelector:
    """One explicit selector from the `status` command line."""

    kind: StatusSelectorKind
    value: str


@dataclass(frozen=True, slots=True)
class StatusOptions:
    """Parsed command options for `status` after CLI normalization."""

    fetch: bool
    selectors: tuple[StatusSelector, ...]
    verbose: bool


@dataclass(frozen=True, slots=True)
class _ResolvedStatusSelector:
    note: object | None
    revset: str | None


def status(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    fetch: bool,
    pull_request: str | Sequence[str] | None,
    repository: Path | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[StatusSelector] | None = None,
    verbose: bool,
) -> int:
    """CLI entrypoint for `status`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    return _run_status(
        context=context,
        options=StatusOptions(
            fetch=fetch,
            selectors=_normalize_status_selectors(
                pull_request=pull_request,
                revset=revset,
                selectors=selectors,
            ),
            verbose=verbose,
        ),
    )


def _run_status(
    *,
    context: CommandContext,
    options: StatusOptions,
) -> int:
    if options.fetch:
        refresh_remote_state_for_status(jj_client=context.jj_client)

    if not options.selectors:
        prepared_status = _prepare_status_with_spinner(
            config=context.config,
            jj_client=context.jj_client,
            revset=None,
        )
        exit_code = _render_prepared_status(
            config=context.config,
            prepared_status=prepared_status,
            verbose=options.verbose,
        )
        _emit_connected_stale_stacks_advisory(
            jj_client=context.jj_client,
            rendered_stacks=(prepared_status.prepared.stack,),
            state=prepared_status.prepared.state,
        )
        return exit_code

    exit_code = 0
    multi_selector = len(options.selectors) > 1
    rendered_stack_keys: set[tuple[object, ...]] = set()
    rendered_stacks: list[LocalStack] = []
    state: ReviewState | None = None
    printed_blocks = 0
    for selector in options.selectors:
        try:
            resolved_selector = _resolve_status_selector(
                jj_client=context.jj_client,
                selector=selector,
            )
            prepared_status = _prepare_status_with_spinner(
                config=context.config,
                jj_client=context.jj_client,
                revset=resolved_selector.revset,
            )
        except CliError as error:
            if printed_blocks:
                console.output("")
            if multi_selector:
                console.output(_status_heading(selector))
            console.warning(t"Error: {error_message(error)}")
            hint = error.hint
            if hint is not None:
                console.warning(t"Hint: {hint}")
            exit_code = 1
            printed_blocks += 1
            continue

        stack_key = _prepared_status_identity(prepared_status)
        if stack_key in rendered_stack_keys:
            continue
        rendered_stack_keys.add(stack_key)
        rendered_stacks.append(prepared_status.prepared.stack)
        state = prepared_status.prepared.state

        if printed_blocks:
            console.output("")
        if multi_selector:
            console.output(_status_heading(selector))
        if resolved_selector.note is not None:
            console.note(resolved_selector.note)
        exit_code = max(
            exit_code,
            _render_prepared_status(
                config=context.config,
                prepared_status=prepared_status,
                verbose=options.verbose,
            ),
        )
        printed_blocks += 1
    if state is not None:
        _emit_connected_stale_stacks_advisory(
            jj_client=context.jj_client,
            rendered_stacks=tuple(rendered_stacks),
            state=state,
        )
    return exit_code


def _emit_connected_stale_stacks_advisory(
    *,
    jj_client: JjClient,
    rendered_stacks: tuple[LocalStack, ...],
    state: ReviewState,
) -> None:
    """Hint that connected stacks changed since their last successful submit.

    This intentionally walks only descendants of the stack(s) status rendered.
    Repo-wide stale-stack warnings belong to `list`; plain `status` should not
    inspect or warn about unrelated review work.
    """

    if not state.changes:
        return
    discovered = discover_connected_tracked_stacks(
        jj_client=jj_client,
        selected_stacks=rendered_stacks,
        state=state,
    )
    rendered_head_change_ids = {stack.head.change_id for stack in rendered_stacks}
    rendered_change_ids = {
        revision.change_id for stack in rendered_stacks for revision in stack.revisions
    }
    other_stacks = tuple(
        stack
        for stack in discovered
        if stack.head.change_id not in rendered_head_change_ids
        and _stack_has_tracked_change_outside_selection(
            stack,
            rendered_change_ids=rendered_change_ids,
            state=state,
        )
    )
    if not other_stacks:
        return
    stale_heads = tuple(
        stack.head.change_id
        for stack in other_stacks
        if submitted_state_disagreement(state, (stack,))
    )
    if not stale_heads:
        return
    if len(stale_heads) == 1:
        head = stale_heads[0][:8]
        console.warning(
            (
                "Other tracked stack has changed since its last submit; ",
                t"inspect with {ui.cmd(f'jj-review status {head}')} or refresh with "
                t"{ui.cmd(f'jj-review submit {head}')}.",
            )
        )
        return
    heads_fragments = ui.join(ui.change_id, stale_heads)
    console.warning(
        (
            "Other tracked stacks have changed since their last submit; ",
            t"inspect with {ui.cmd('jj-review status <head>')} or refresh with "
            t"{ui.cmd('jj-review submit <head>')}: ",
            *heads_fragments,
        )
    )


def _stack_has_tracked_change_outside_selection(
    stack: LocalStack,
    *,
    rendered_change_ids: set[str],
    state: ReviewState,
) -> bool:
    for revision in stack.revisions:
        if revision.change_id in rendered_change_ids:
            continue
        cached_change = state.changes.get(revision.change_id)
        if cached_change is None:
            continue
        change_status = classify_saved_review_change(cached_change, local="present")
        if change_status.saved_review_identity or change_status.link == "unlinked":
            return True
    return False


def _normalize_status_selectors(
    *,
    pull_request: str | Sequence[str] | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[StatusSelector] | None,
) -> tuple[StatusSelector, ...]:
    if selectors is not None:
        return tuple(selectors)

    ordered: list[StatusSelector] = []
    if pull_request is not None:
        if isinstance(pull_request, str):
            ordered.append(StatusSelector(kind="pull_request", value=pull_request))
        else:
            ordered.extend(
                StatusSelector(kind="pull_request", value=value) for value in pull_request
            )
    if revset is not None:
        if isinstance(revset, str):
            ordered.append(StatusSelector(kind="revset", value=revset))
        else:
            ordered.extend(StatusSelector(kind="revset", value=value) for value in revset)
    return tuple(ordered)


def _resolve_status_selector(
    *,
    jj_client: JjClient,
    selector: StatusSelector,
) -> _ResolvedStatusSelector:
    if selector.kind == "pull_request":
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="status",
            jj_client=jj_client,
            pull_request_reference=selector.value,
            revset=None,
        )
        return _ResolvedStatusSelector(
            note=t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}",
            revset=resolved_revset,
        )
    return _ResolvedStatusSelector(
        note=None,
        revset=resolve_selected_revset(
            command_label="status",
            default_revset=None,
            require_explicit=False,
            revset=selector.value,
        ),
    )


def _prepare_status_for_revset(
    *,
    config: RepoConfig,
    jj_client: JjClient,
    revset: str | None,
):
    try:
        return prepare_status(
            config=config,
            fetch_remote_state=False,
            jj_client=jj_client,
            persist_bookmarks=False,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error


def _prepare_status_with_spinner(
    *,
    config: RepoConfig,
    jj_client: JjClient,
    revset: str | None,
):
    with console.spinner(description="Inspecting jj stack"):
        return _prepare_status_for_revset(
            config=config,
            jj_client=jj_client,
            revset=revset,
        )


def _prepared_status_identity(prepared_status) -> tuple[object, ...]:
    change_ids = tuple(
        revision.revision.change_id for revision in prepared_status.prepared.status_revisions
    )
    return (
        prepared_status.prepared.stack.base_parent.commit_id,
        *change_ids,
    )


def _status_heading(selector: StatusSelector) -> object:
    if selector.kind == "pull_request":
        return f"Status for PR {selector.value}:"
    return t"Status for {ui.revset(selector.value)}:"


def _render_prepared_status(
    *,
    config: RepoConfig,
    prepared_status,
    verbose: bool,
) -> int:
    selection_lines = render_status_selection_lines(prepared_status=prepared_status)
    if selection_lines:
        _emit_lines(selection_lines, emitter=console.warning)

    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        result = stream_status(
            lock_cache_update=True,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    if getattr(result, "cache_update_skipped", False):
        console.warning("Cache not refreshed: another jj-review operation is running.")

    github_lines = render_status_github_lines(
        github_error=result.github_error,
        github_repository=result.github_repository,
    )
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
    _emit_lines(render_status_advisory_lines(config=config, result=result))
    _emit_lines(render_status_operation_lines(prepared_status=prepared_status))

    exit_code = 1 if result.incomplete else 0
    if any(
        _interrupted_operation_blocks_status(
            loaded=loaded,
            prepared_status=prepared_status,
        )
        for loaded in prepared_status.outstanding_operations
    ):
        exit_code = max(exit_code, 1)
    return exit_code


def render_status_selection_lines(*, prepared_status) -> tuple[object, ...]:
    """Render exceptional local selection context lines."""

    prepared = prepared_status.prepared
    lines: list[object] = []
    if prepared.remote is None:
        lines.append(remote_unavailable_message(remote_error=prepared.remote_error))
    return tuple(lines)


def render_status_github_lines(
    *,
    github_error: ErrorMessage | None,
    github_repository: str | None,
) -> tuple[object, ...]:
    """Render GitHub availability lines as status streaming begins."""

    lines: list[object] = []
    github_message = github_unavailable_message(
        github_error=github_error,
        github_repository=github_repository,
    )
    if github_message is not None:
        lines.append(github_message)
    return tuple(lines)


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

    unsubmitted_revisions = tuple(
        revision
        for revision in result.revisions
        if _classify_revision_for_summary(revision, github_available=github_available)
        == "unsubmitted"
    )
    submitted_revisions = tuple(
        revision
        for revision in result.revisions
        if _classify_revision_for_summary(revision, github_available=github_available)
        == "submitted"
    )

    lines: list[str] = []
    unsubmitted_lines = _render_summary_section(
        "Unsubmitted stack",
        include_leading_separator=leading_separator,
        revisions=unsubmitted_revisions,
        verbose=verbose,
        renderer=lambda revision: _render_summary_revision_lines(
            client=client,
            revision=revision,
            github_available=github_available,
            show_status=False,
            verbose=verbose,
            prerendered_blocks=prerendered_blocks,
        ),
    )
    if unsubmitted_lines:
        lines.extend(unsubmitted_lines)

    submitted_lines = _render_summary_section(
        _render_submitted_section_title(submitted_revisions),
        include_leading_separator=False,
        revisions=submitted_revisions,
        verbose=verbose,
        renderer=lambda revision: _render_summary_revision_lines(
            client=client,
            revision=revision,
            github_available=github_available,
            show_status=True,
            verbose=verbose,
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
    prepared,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render the trunk footer with native `jj log` formatting."""

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
    prepared_status,
) -> tuple[object, ...]:
    """Render the empty-stack footer and explanation."""

    return (
        *render_trunk_status_lines(
            prepared=prepared_status.prepared,
        ),
        "The selected stack has no changes to review.",
    )


def _prefetch_revision_log_blocks(
    *,
    client,
    revisions,
    trunk,
) -> dict[str, tuple[str, ...]]:
    """Render the `jj log` block for every revision we will print, in parallel."""

    seen: set[str] = set()
    ordered: list[object] = []
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
    config: RepoConfig,
    result: StatusResult,
) -> tuple[object, ...]:
    """Render any advisories that follow the status stack output."""

    cleanup_revisions = [
        revision
        for revision in result.revisions
        if classify_review_status_revision(revision).pr_lifecycle == "merged"
    ]
    divergent_revisions = [
        revision
        for revision in result.revisions
        if classify_review_status_revision(revision).local == "divergent"
        and classify_review_status_revision(revision).pr_lifecycle != "merged"
    ]
    link_revisions = [
        revision for revision in result.revisions if _revision_has_link_advisory(revision)
    ]
    submitted_disagreements = result.submitted_state_disagreements
    policy_warning_rows: list[tuple[object, object]] = []
    for revision in cleanup_revisions:
        lookup = revision.pull_request_lookup
        pull_request = lookup.pull_request if lookup is not None else None
        if pull_request is None:
            continue
        base_ref = pull_request.base.ref
        if not is_review_bookmark(base_ref, prefix=config.bookmark_prefix):
            continue
        policy_warning_rows.append(
            (
                "Repository policy",
                t"Repository policy warning: PR #{pull_request.number} merged into "
                t"{ui.bookmark(base_ref)}; configure GitHub to block merges of PRs "
                t"targeting {ui.bookmark(bookmark_glob(config.bookmark_prefix))}",
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

    rows: list[tuple[object, object]] = []
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
                        ui.cmd("jj-review submit"),
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
                        ui.cmd("jj-review submit"),
                        " ",
                        ui.revset(result.selected_revset),
                    ),
                )
            )
        rows.extend(_submitted_state_disagreement_rows(submitted_disagreements))

    if cleanup_revisions:
        rows.append(
            (
                "Cleanup needed",
                "Submit note: descendant PR bases still follow the old local ancestry "
                "until the remaining local changes are rebased",
            )
        )
        rows.append(
            (
                "Next step",
                (
                    ui.cmd("jj-review cleanup --rebase"),
                    " ",
                    ui.revset(result.selected_revset),
                    " or ",
                    ui.cmd("jj-review cleanup --rebase --dry-run"),
                    " ",
                    ui.revset(result.selected_revset),
                ),
            )
        )
        for revision in cleanup_revisions:
            pull_request_number = revision.pull_request_number()
            pull_request_label = (
                f"PR #{pull_request_number}" if pull_request_number is not None else "merged PR"
            )
            rows.append(
                (
                    ui.change_id(revision.change_id),
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
                    ui.change_id(revision.change_id),
                    _describe_link_advisory(revision),
                )
            )

    rows.extend(policy_warning_rows)

    for revision in divergent_revisions:
        rows.append(
            (
                ui.change_id(revision.change_id),
                t"Resolve the multiple visible revisions for this change before retrying "
                t"({ui.cmd('jj log -r')} {ui.revset(f'change_id({revision.change_id})')})",
            )
        )
    return ("", "Advisories:", _advisory_table(tuple(rows)))


def _submitted_state_disagreement_rows(
    disagreements: Sequence[SubmittedStateDisagreement],
) -> tuple[tuple[object, object], ...]:
    commit_changed = tuple(
        disagreement.change_id for disagreement in disagreements if disagreement.commit_changed
    )
    parent_changed = tuple(
        disagreement.change_id for disagreement in disagreements if disagreement.parent_changed
    )
    stack_head_changed = tuple(
        disagreement.change_id
        for disagreement in disagreements
        if disagreement.stack_head_changed
    )
    rows: list[tuple[object, object]] = []
    if commit_changed:
        rows.append(
            (
                "New commit IDs",
                _format_submit_baseline_reason(
                    change_ids=commit_changed,
                    noun="change",
                ),
            )
        )
    if parent_changed:
        rows.append(
            (
                "New PR bases",
                _format_submit_baseline_reason(
                    change_ids=parent_changed,
                    noun="change",
                ),
            )
        )
    if stack_head_changed:
        rows.append(
            (
                "New stack head",
                _format_submit_baseline_reason(
                    change_ids=stack_head_changed,
                    noun="change",
                ),
            )
        )
    return tuple(rows)


def _format_submit_baseline_reason(
    *,
    change_ids: Sequence[str],
    noun: str,
) -> object:
    if len(change_ids) == 1:
        return ui.change_id(change_ids[0])
    plural_noun = f"{noun}s" if len(change_ids) != 1 else noun
    return (f"{len(change_ids)} {plural_noun}: ", *_format_change_id_list(change_ids))


def _format_change_id_list(change_ids: Sequence[str], *, limit: int = 5) -> tuple[object, ...]:
    visible = tuple(change_ids[:limit])
    rendered = list(ui.join(ui.change_id, visible))
    remaining = len(change_ids) - limit
    if remaining > 0:
        if rendered:
            rendered.append(", ")
        rendered.append(f"... {remaining} more")
    return tuple(rendered)


def _advisory_table(rows: tuple[tuple[object, object], ...]) -> ui.DataTable:
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
    link_revisions: tuple[object, ...],
    selected_revset: str,
) -> tuple[object, object]:
    states = {_link_advisory_kind(revision) for revision in link_revisions}
    change_phrase = _link_advisory_change_phrase(link_revisions)
    restart_submit_command = ui.cmd(f"jj-review submit --restart {selected_revset}")
    if states == {"closed"}:
        label = (
            "Closed GitHub PR"
            if len(link_revisions) == 1
            else "Closed GitHub PRs"
        )
        closed_phrase = "a closed PR" if len(link_revisions) == 1 else "closed PRs"
        detail = (
            f"GitHub reports {closed_phrase} for {change_phrase}; submit will not "
            "reuse closed reviews. Reopen the PR on GitHub to continue that review, "
            "relink an open replacement, or run ",
            restart_submit_command,
            " to create fresh PRs.",
        )
        return label, detail
    if states == {"missing"}:
        label = "Missing GitHub PR" if len(link_revisions) == 1 else "Missing GitHub PRs"
        detail = (
            "GitHub did not report a PR for the remembered review branch of "
            f"{change_phrase}. Run ",
            ui.cmd("jj-review status --fetch <change>"),
            " if branch state may be stale. Relink an open PR if one exists; "
            "otherwise run ",
            restart_submit_command,
            " to create fresh PRs.",
        )
        return label, detail
    if states == {"ambiguous"}:
        label = (
            "Ambiguous GitHub PR"
            if len(link_revisions) == 1
            else "Ambiguous GitHub PRs"
        )
        detail = (
            "GitHub reports multiple PRs for the remembered review branch of "
            f"{change_phrase}. Run ",
            ui.cmd("jj-review status --fetch <change>"),
            " to refresh, then relink the intended open PR.",
        )
        return label, detail
    if states == {"remembered"}:
        label = "Saved GitHub PR" if len(link_revisions) == 1 else "Saved GitHub PRs"
        detail = (
            "GitHub found the remembered PR, but its head branch no longer matches "
            f"{change_phrase}. Relink it if that PR should stay attached; "
            "otherwise run ",
            restart_submit_command,
            " to create fresh PRs.",
        )
        return label, detail
    detail = (
        "GitHub reports closed, missing, or ambiguous PR state for one or more "
        "changes shown above. Inspect the per-change rows, then reopen, relink, or run ",
        restart_submit_command,
        " as appropriate.",
    )
    return "GitHub PRs need repair", detail


def _link_advisory_change_phrase(link_revisions: tuple[object, ...]) -> str:
    if len(link_revisions) == 1:
        return "the change shown above"
    return "one or more changes shown above"


def _link_advisory_kind(revision) -> str:
    lookup = revision.pull_request_lookup
    if lookup is None:
        raise AssertionError("Link advisory requires a pull request lookup.")
    change_status = classify_review_status_revision(revision)
    if getattr(lookup, "source", "head") == "remembered" and lookup.message is not None:
        return "remembered"
    if change_status.pr_lifecycle in {"ambiguous", "closed", "missing"}:
        return change_status.pr_lifecycle
    raise AssertionError(f"Unexpected link advisory state: {change_status.pr_lifecycle}")


def render_status_operation_lines(*, prepared_status) -> tuple[object, ...]:
    """Render any stale or incomplete operation notices."""

    lines: list[object] = []
    if prepared_status.stale_operations:
        lines.extend(("", "Stale incomplete operations (change IDs no longer in repo):"))
        for loaded in prepared_status.stale_operations:
            alive = pid_is_alive(loaded.operation.pid)
            status_str = "process alive" if alive else "process dead"
            lines.append(
                _prefixed_operation_line(
                    _render_operation_description(loaded.operation),
                    format_status_annotation(f"{status_str}, {loaded.path.name}"),
                )
            )

    if prepared_status.outstanding_operations:
        lines.extend(("", "Interrupted operations recorded:"))
        for loaded in prepared_status.outstanding_operations:
            lines.extend(
                _render_interrupted_operation_block(
                    loaded=loaded,
                    prepared_status=prepared_status,
                )
            )
    return tuple(lines)


def _interrupted_operation_blocks_status(*, loaded, prepared_status) -> bool:
    """Return True when an interrupted operation should make `status` exit nonzero."""

    if pid_is_alive(loaded.operation.pid):
        return True

    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )

    if isinstance(loaded.operation, SubmitOperationRecord):
        decision = submit_status_decision(
            operation=loaded.operation,
            current_change_ids=current_change_ids,
            current_commit_ids=current_commit_ids,
            current_identity=_current_submit_identity(prepared_status=prepared_status),
        )
        return decision is SubmitStatusDecision.INSPECT

    if isinstance(loaded.operation, CleanupRebaseOperationRecord):
        return (
            match_cleanup_rebase_operation(
                operation=loaded.operation,
                current_change_ids=current_change_ids,
                current_commit_ids=current_commit_ids,
            )
            == "overlap"
        )

    if isinstance(loaded.operation, CloseOperationRecord):
        return (
            match_close_operation(
                operation=loaded.operation,
                current_change_ids=current_change_ids,
                current_commit_ids=current_commit_ids,
            )
            == "overlap"
        )

    current_change_id_set = set(current_change_ids)
    return bool(loaded.operation.change_ids() & current_change_id_set)


def _render_interrupted_operation_block(
    *,
    loaded,
    prepared_status,
) -> tuple[object, ...]:
    operation = loaded.operation
    header = _render_interrupted_operation_header(operation)
    lines: list[object] = [("  ", header)]

    if pid_is_alive(operation.pid):
        lines.append(
            (
                "    ",
                t"still in progress (PID {operation.pid}); run "
                t"{ui.cmd('jj-review status')} again after it finishes",
            )
        )
        return tuple(lines)

    if isinstance(operation, SubmitOperationRecord):
        detail_lines = _interrupted_submit_detail_lines(
            operation=operation,
            prepared_status=prepared_status,
        )
    elif isinstance(operation, CleanupRebaseOperationRecord | CloseOperationRecord):
        detail_lines = _interrupted_ordered_detail_lines(
            operation=operation,
            match=_match_ordered_operation(
                operation=operation,
                prepared_status=prepared_status,
            ),
        )
    elif isinstance(operation, LandOperationRecord):
        detail_lines = _interrupted_ordered_detail_lines(
            operation=operation,
            match=_match_land_operation(operation=operation, prepared_status=prepared_status),
        )
    elif isinstance(operation, RelinkOperationRecord):
        detail_lines = (
            (
                "inspect with ",
                _render_status_command(operation),
                " before rerunning ",
                ui.cmd("jj-review relink"),
            ),
        )
    elif isinstance(operation, CleanupOperationRecord):
        detail_lines = (
            (
                "inspect with ",
                _render_status_command(operation),
                "; rerun ",
                ui.cmd("jj-review cleanup"),
                " if still needed",
            ),
        )
    else:
        detail_lines = (("inspect with ", ui.cmd("jj-review status")),)

    lines.extend(("    ", detail) for detail in detail_lines)
    return tuple(lines)


def _interrupted_submit_detail_lines(
    *,
    operation: SubmitOperationRecord,
    prepared_status,
) -> tuple[object, ...]:
    if (
        _recorded_stack_head_visible(
            operation=operation,
            prepared_status=prepared_status,
        )
        is False
    ):
        return (
            (
                "change ",
                ui.change_id(_operation_selector(operation) or operation.display_revset),
                " from this interrupted submit is no longer visible in jj",
            ),
            (
                "preview clearing this notice with ",
                ui.cmd("jj-review abort --dry-run"),
                "; clear it with ",
                ui.cmd("jj-review abort"),
            ),
        )

    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    decision = submit_status_decision(
        operation=operation,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
        current_identity=_current_submit_identity(prepared_status=prepared_status),
    )
    resume_command = _render_resume_command(operation)
    abort_command = ui.cmd("jj-review abort --dry-run")
    if decision is SubmitStatusDecision.CONTINUE:
        return (
            "this matches the stack shown above",
            ("continue with ", resume_command, ", or preview backout with ", abort_command),
        )
    if decision is SubmitStatusDecision.CURRENT_STACK:
        return (
            "the recorded stack was rewritten; rerunning submit will use the stack shown above",
            ("continue with ", resume_command, ", or preview backout with ", abort_command),
        )
    if decision is SubmitStatusDecision.INSPECT:
        return (
            "this matches the stack shown above, but the recorded submit target is different",
            (
                "inspect before continuing with ",
                resume_command,
                "; preview backout with ",
                abort_command,
            ),
        )
    return (
        "this is not the stack shown above",
        ("inspect with ", _render_status_command(operation)),
        ("finish with ", resume_command, ", or preview backout with ", abort_command),
    )


def _current_submit_identity(*, prepared_status) -> SubmitRecoveryIdentity | None:
    current_remote = prepared_status.prepared.remote
    current_github_repository = prepared_status.github_repository
    if current_remote is None or current_github_repository is None:
        return None
    return SubmitRecoveryIdentity.from_github_repository(
        remote_name=current_remote.name,
        github_repository=current_github_repository,
    )


def _recorded_stack_head_visible(
    *,
    operation: SubmitOperationRecord,
    prepared_status,
) -> bool | None:
    """Return whether the recorded submit head still resolves, when status can tell."""

    if not operation.ordered_change_ids:
        return None
    client = getattr(prepared_status.prepared, "client", None)
    if client is None:
        return None
    head_change_id = operation.ordered_change_ids[-1]
    revisions = client.query_revisions_by_change_ids((head_change_id,)).get(head_change_id, ())
    return bool(revisions)


def _operation_rerun_command(
    operation: (
        SubmitOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | LandOperationRecord
    ),
) -> str:
    if isinstance(operation, SubmitOperationRecord):
        return "submit"
    if isinstance(operation, CleanupRebaseOperationRecord):
        return "cleanup --rebase"
    if isinstance(operation, LandOperationRecord):
        return "land"
    return "close --cleanup" if operation.cleanup else "close"


def _match_ordered_operation(
    *,
    operation: CleanupRebaseOperationRecord | CloseOperationRecord,
    prepared_status,
) -> OrderedOperationMatch:
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    if isinstance(operation, CleanupRebaseOperationRecord):
        return match_cleanup_rebase_operation(
            operation=operation,
            current_change_ids=current_change_ids,
            current_commit_ids=current_commit_ids,
        )
    return match_close_operation(
        operation=operation,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def _match_land_operation(
    *,
    operation: LandOperationRecord,
    prepared_status,
) -> OrderedOperationMatch:
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    if operation.ordered_change_ids == current_change_ids:
        if operation.ordered_commit_ids and operation.ordered_commit_ids == current_commit_ids:
            return "exact"
        return "same-logical"
    if set(operation.ordered_change_ids) == set(current_change_ids):
        return "same-logical"
    if set(operation.ordered_change_ids).issubset(current_change_ids):
        return "covered"
    if set(current_change_ids).issubset(operation.ordered_change_ids):
        return "trimmed"
    if set(operation.ordered_change_ids) & set(current_change_ids):
        return "overlap"
    return "disjoint"


def _interrupted_ordered_detail_lines(
    *,
    match: OrderedOperationMatch,
    operation: CleanupRebaseOperationRecord | CloseOperationRecord | LandOperationRecord,
) -> tuple[object, ...]:
    resume_command = _render_resume_command(operation)

    if match == "exact":
        return (
            "this matches the stack shown above",
            ("continue with ", resume_command),
        )
    if match == "same-logical":
        command = _operation_rerun_command(operation)
        return (
            (
                "the recorded stack was rewritten; rerunning ",
                command,
                " will use the stack shown above",
            ),
            ("continue with ", resume_command),
        )
    if match == "covered":
        return (
            "the recorded changes are all included in the stack shown above",
            ("continue with ", resume_command),
        )
    if match == "trimmed":
        return (
            "the recorded stack includes changes that are no longer in the stack shown above",
            (
                "inspect with ",
                _render_status_command(operation),
                " before continuing with ",
                resume_command,
            ),
        )
    if match == "overlap":
        return (
            "the recorded stack partly overlaps the stack shown above",
            (
                "inspect with ",
                _render_status_command(operation),
                " before continuing with ",
                resume_command,
            ),
        )
    return (
        "this is not the stack shown above",
        ("inspect with ", _render_status_command(operation)),
        ("finish with ", resume_command),
    )


def _render_resume_command(
    operation: (
        SubmitOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | LandOperationRecord
    ),
) -> ui.SemanticText:
    selector = _operation_selector(operation)
    command = f"jj-review {_operation_rerun_command(operation)}"
    if selector is not None:
        command = f"{command} {selector}"
    return ui.cmd(command)


def _render_status_command(operation) -> ui.SemanticText:
    selector = _operation_selector(operation)
    command = "jj-review status"
    if selector is not None:
        command = f"{command} {selector}"
    return ui.cmd(command)


def _operation_selector(operation) -> str | None:
    if isinstance(
        operation,
        SubmitOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | LandOperationRecord,
    ):
        if operation.ordered_change_ids:
            return short_change_id(operation.ordered_change_ids[-1])
    if isinstance(operation, RelinkOperationRecord):
        return short_change_id(operation.change_id)
    return None


def _render_interrupted_operation_header(operation) -> object:
    started = _render_started_at(operation)
    if isinstance(
        operation,
        SubmitOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | LandOperationRecord,
    ):
        return (
            _render_operation_command(operation),
            " for ",
            _render_recorded_stack_head(operation),
            ", ",
            started,
            " from ",
            ui.revset(operation.display_revset),
        )
    if isinstance(operation, RelinkOperationRecord):
        return (
            _render_operation_command(operation),
            " for ",
            ui.change_id(operation.change_id),
            ", ",
            started,
        )
    return (_render_operation_command(operation), ", ", started)


def _render_operation_command(operation) -> object:
    if isinstance(operation, SubmitOperationRecord):
        return ui.cmd("submit")
    if isinstance(operation, CleanupRebaseOperationRecord):
        return ui.cmd("cleanup --rebase")
    if isinstance(operation, CloseOperationRecord):
        return ui.cmd("close --cleanup" if operation.cleanup else "close")
    if isinstance(operation, LandOperationRecord):
        return ui.cmd("land")
    if isinstance(operation, RelinkOperationRecord):
        return ui.cmd("relink")
    if isinstance(operation, CleanupOperationRecord):
        return ui.cmd("cleanup")
    return operation.label


def _render_recorded_stack_head(
    operation: (
        SubmitOperationRecord
        | CleanupRebaseOperationRecord
        | CloseOperationRecord
        | LandOperationRecord
    ),
) -> object:
    if not operation.ordered_change_ids:
        return "stack"
    return ui.change_id(operation.ordered_change_ids[-1])


def _render_started_at(operation) -> str:
    started_at = getattr(operation, "started_at", None)
    if not isinstance(started_at, str):
        return "started at unknown time"
    return f"started {_format_operation_age(started_at)}"


def _format_operation_age(
    started_at: str,
    *,
    now: datetime | None = None,
) -> str:
    if now is None:
        now = _now_utc()
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return "at unknown time"
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    else:
        started = started.astimezone(UTC)

    elapsed_seconds = int((now - started).total_seconds())
    if elapsed_seconds < 0:
        return started.date().isoformat()
    if elapsed_seconds < 60:
        return "just now"
    elapsed_minutes = elapsed_seconds // 60
    if elapsed_minutes < 60:
        return f"{elapsed_minutes}m ago"
    elapsed_hours = elapsed_minutes // 60
    if elapsed_hours < 24:
        return f"{elapsed_hours}h ago"
    elapsed_days = elapsed_hours // 24
    if elapsed_days < 7:
        return f"{elapsed_days}d ago"
    return started.date().isoformat()


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _render_summary_revision_lines(
    *,
    client,
    revision,
    github_available: bool,
    show_status: bool,
    verbose: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render one revision inside a submitted or unsubmitted summary section."""

    summary = _format_status_summary(revision, github_available=github_available)
    if not show_status and summary == "not submitted":
        summary = None
    return render_revision_lines(
        client=client,
        revision=revision,
        bookmark=None if verbose else revision.bookmark,
        suffix=summary,
        prerendered_lines=(
            prerendered_blocks.get(revision.commit_id) if prerendered_blocks else None
        ),
    )


def _classify_revision_for_summary(
    revision,
    *,
    github_available: bool,
) -> str:
    """Classify a revision into submitted, unsubmitted, or other."""

    change_status = classify_review_status_revision(revision)
    if change_status.link == "unlinked":
        return "submitted"

    if change_status.pr_lifecycle == "none" and not change_status.pr_lookup_error:
        if _has_cached_review_identity(revision.cached_change):
            return "submitted"
        return "unsubmitted"

    if change_status.pr_lifecycle in {"open", "closed", "merged"}:
        return "submitted"
    if change_status.pr_lifecycle == "missing":
        if _has_cached_review_identity(revision.cached_change):
            return "submitted"
        return "unsubmitted"
    if change_status.pr_lifecycle == "ambiguous" or change_status.pr_lookup_error:
        if _has_cached_review_identity(revision.cached_change):
            return "submitted"
        return "unsubmitted"
    return "unsubmitted"


def _has_cached_review_identity(cached_change: CachedChange | None) -> bool:
    return classify_saved_review_change(
        cached_change,
        local="present",
    ).saved_review_identity


def _format_status_summary(revision, *, github_available: bool) -> str:
    lookup = revision.pull_request_lookup
    cached_change = revision.cached_change
    cached_label = _format_cached_pull_request_label(cached_change)
    change_status = classify_review_status_revision(revision)
    summary: str
    if change_status.link == "unlinked":
        if lookup is not None and lookup.pull_request is not None:
            pull_request = lookup.pull_request
            if pull_request.state == "open":
                summary = format_pull_request_label(
                    pull_request.number,
                    is_draft=pull_request.is_draft,
                    prefix="unlinked ",
                )
            else:
                summary = f"unlinked PR #{pull_request.number} {pull_request.state}"
        elif change_status.remote_branch != "absent":
            summary = "unlinked branch"
        else:
            summary = "unlinked"
    elif change_status.pr_lifecycle == "none" and not change_status.pr_lookup_error:
        if cached_label is not None:
            summary = cached_label
        elif _has_cached_review_identity(cached_change):
            summary = "submitted, GitHub status unknown"
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
        if review_decision == "none" and lookup.review_decision_error is not None:
            review_decision = (
                "none" if cached_change is None else cached_change.pr_review_decision or "none"
            )
        if change_status.pr_draft is True:
            pass
        elif review_decision == "approved":
            summary = f"{summary} approved"
        elif review_decision == "changes_requested":
            summary = f"{summary} changes requested"
    elif change_status.pr_lifecycle == "missing":
        if cached_label is not None:
            summary = f"{cached_label}, no PR found for branch"
        elif _has_cached_review_identity(cached_change):
            summary = "submitted, no PR found for branch"
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
            summary = (
                f"{pr_label} merged into {lookup.pull_request.base.ref}, cleanup needed"
            )
        else:
            summary = f"{pr_label} closed"
    else:
        message = (
            lookup.message
            if lookup is not None and lookup.message is not None
            else "GitHub lookup failed"
        )
        if cached_label is not None:
            summary = f"{cached_label}, {message}"
        else:
            summary = message

    if change_status.local == "divergent" and change_status.pr_lifecycle != "merged":
        summary = f"{summary}, multiple visible revisions"

    managed_comments_lookup = revision.managed_comments_lookup
    if managed_comments_lookup is not None and managed_comments_lookup.state in {
        "ambiguous",
        "error",
    }:
        message = managed_comments_lookup.message or "stack comment lookup failed"
        return f"{summary}, {message}"
    return summary


def _format_live_pull_request_label(
    *,
    lookup,
    pull_request_number: int,
    is_draft: bool,
) -> str:
    prefix = "remembered " if getattr(lookup, "source", "head") == "remembered" else ""
    return format_pull_request_label(
        pull_request_number,
        is_draft=is_draft,
        prefix=prefix,
    )


def _emit_lines(
    lines: tuple[object, ...], *, emitter=console.output, soft_wrap: bool = True
) -> None:
    for line in lines:
        emitter(line, soft_wrap=soft_wrap)


def _format_cached_pull_request_label(cached_change) -> str | None:
    if cached_change is None or cached_change.pr_number is None:
        return None

    label = format_pull_request_label(
        cached_change.pr_number,
        is_draft=bool(cached_change.pr_is_draft) and cached_change.pr_state == "open",
        prefix="saved ",
    )
    if cached_change.pr_state is None:
        return label

    details = [cached_change.pr_state]
    if (
        cached_change.pr_state == "open"
        and not cached_change.pr_is_draft
        and cached_change.pr_review_decision is not None
    ):
        _rd = cached_change.pr_review_decision
        details.append("changes requested" if _rd == "changes_requested" else _rd)
    return f"{label} ({', '.join(details)})"


def _prefixed_operation_line(description: object, status: object) -> object:
    return ui.prefixed_line("  ", (description, "  ", status))


def _render_operation_description(operation) -> object:
    if isinstance(operation, CleanupOperationRecord):
        return ui.cmd("cleanup")
    return describe_operation(operation)


def _revision_has_link_advisory(revision) -> bool:
    change_status = classify_review_status_revision(revision)
    if change_status.link == "unlinked":
        return False
    lookup = revision.pull_request_lookup
    if lookup is None:
        return False
    if getattr(lookup, "source", "head") == "remembered" and lookup.message is not None:
        return True
    if change_status.pr_lifecycle == "ambiguous":
        return True
    if change_status.pr_lifecycle == "missing":
        return change_status.has_stale_pull_request_link
    if change_status.pr_lifecycle == "closed":
        return lookup.pull_request is not None
    return False


def _describe_link_advisory(revision) -> object:
    lookup = revision.pull_request_lookup
    if lookup is None:
        raise AssertionError("Link advisory requires a pull request lookup.")
    change_status = classify_review_status_revision(revision)
    if getattr(lookup, "source", "head") == "remembered" and lookup.message is not None:
        return lookup.message
    if change_status.pr_lifecycle == "ambiguous":
        return lookup.message or "GitHub reports more than one matching pull request"
    if change_status.pr_lifecycle == "missing":
        cached_change = revision.cached_change
        if cached_change is not None and cached_change.pr_number is not None:
            return (
                f"GitHub did not report remembered PR #{cached_change.pr_number} "
                "for this branch"
            )
        cached_label = _format_cached_pull_request_label(cached_change)
        if cached_label is None:
            return "GitHub did not report a pull request for this branch"
        return f"GitHub did not report {cached_label} for this branch"
    if change_status.pr_lifecycle == "closed":
        pull_request = lookup.pull_request
        if pull_request is None:
            raise AssertionError("Closed pull request advisory requires a pull request.")
        return (
            f"PR #{pull_request.number} is {pull_request.state}; submit will not reuse a "
            "closed review automatically"
        )
    raise AssertionError(f"Unexpected link advisory state: {change_status.pr_lifecycle}")
