"""Show how the selected jj stack(s) currently appear locally and on GitHub.

By default it summarizes the submitted and unsubmitted changes in each selected stack;
`--verbose` expands those summaries and includes any bookmark names.

`--fetch` runs a fetch first so the report uses current remote branch locations. Use one or more
revsets and `--pull-request` selectors to inspect several stacks in one run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import jj_review.console as console
import jj_review.ui as ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.commands._stale_stacks import emit_stale_stacks_advisory
from jj_review.config import RepoConfig
from jj_review.errors import CliError, error_message
from jj_review.formatting import (
    NativeRevision,
    NativeRevisionRenderClient,
    format_pull_request_label,
    render_revision_blocks,
    render_revision_lines,
)
from jj_review.github.error_messages import (
    github_unavailable_message,
    remote_unavailable_message,
)
from jj_review.jj.client import JjCliArgs, UnsupportedStackError
from jj_review.models.review_state import ReviewState
from jj_review.models.stack import LocalStack
from jj_review.review.bookmarks import bookmark_glob, is_review_bookmark
from jj_review.review.change_status import (
    ReviewChangeStatus,
    SubmittedStateDisagreement,
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_review.review.discovery import discover_connected_tracked_stacks
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_review.review.status import (
    PreparedStack,
    PreparedStatus,
    PullRequestLookup,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    refresh_remote_state_for_status,
    status_preparation_cli_error,
    stream_status,
)

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
class _ResolvedStatusSelector:
    note: ui.Message | None
    revset: str | None


@dataclass(frozen=True, slots=True)
class _ClassifiedStatusRevision:
    """Rendered status revision paired with its derived review status."""

    revision: ReviewStatusRevision
    status: ReviewChangeStatus


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
        fetch=fetch,
        selectors=_normalize_status_selectors(
            pull_request=pull_request,
            revset=revset,
            selectors=selectors,
        ),
        verbose=verbose,
    )


def _run_status(
    *,
    context: CommandContext,
    fetch: bool,
    selectors: tuple[StatusSelector, ...],
    verbose: bool,
) -> int:
    if fetch:
        refresh_remote_state_for_status(jj_client=context.jj_client)

    if not selectors:
        prepared_status = _prepare_status_with_spinner(
            context=context,
            revset=None,
        )
        exit_code = _render_prepared_status(
            context=context,
            prepared_status=prepared_status,
            verbose=verbose,
        )
        _emit_connected_stale_stacks_advisory(
            context=context,
            rendered_stacks=(prepared_status.prepared.stack,),
            state=prepared_status.prepared.state,
        )
        return exit_code

    exit_code = 0
    multi_selector = len(selectors) > 1
    rendered_stack_keys: set[tuple[str, ...]] = set()
    rendered_stacks: list[LocalStack] = []
    state: ReviewState | None = None
    printed_blocks = 0
    for selector in selectors:
        try:
            resolved_selector = _resolve_status_selector(
                context=context,
                selector=selector,
            )
            prepared_status = _prepare_status_with_spinner(
                context=context,
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
                context=context,
                prepared_status=prepared_status,
                verbose=verbose,
            ),
        )
        printed_blocks += 1
    if state is not None:
        _emit_connected_stale_stacks_advisory(
            context=context,
            rendered_stacks=tuple(rendered_stacks),
            state=state,
        )
    return exit_code


def _emit_connected_stale_stacks_advisory(
    *,
    context: CommandContext,
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
        jj_client=context.jj_client,
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
    emit_stale_stacks_advisory(
        stacks=other_stacks,
        state=state,
        single_subject="Other tracked stack",
        plural_subject="Other tracked stacks",
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
    context: CommandContext,
    selector: StatusSelector,
) -> _ResolvedStatusSelector:
    if selector.kind == "pull_request":
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="status",
            jj_client=context.jj_client,
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
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus:
    try:
        return prepare_status(
            context=context,
            fetch_remote_state=False,
            persist_bookmarks=False,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error


def _prepare_status_with_spinner(
    *,
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus:
    with console.spinner(description="Inspecting jj stack"):
        return _prepare_status_for_revset(
            context=context,
            revset=revset,
        )


def _prepared_status_identity(prepared_status: PreparedStatus) -> tuple[str, ...]:
    change_ids = tuple(
        revision.revision.change_id for revision in prepared_status.prepared.status_revisions
    )
    return (
        prepared_status.prepared.stack.base_parent.commit_id,
        *change_ids,
    )


def _status_heading(selector: StatusSelector) -> ui.Message:
    if selector.kind == "pull_request":
        return f"Status for PR {selector.value}:"
    return t"Status for {ui.revset(selector.value)}:"


def _render_prepared_status(
    *,
    context: CommandContext,
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
            lock_cache_update=True,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    if result.cache_update_skipped:
        console.warning("Cache not refreshed: another jj-review operation is running.")

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
    _emit_lines(render_status_advisory_lines(config=context.config, result=result))

    return 1 if result.incomplete else 0


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
            verbose=verbose,
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
    prepared: PreparedStack,
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
    client: NativeRevisionRenderClient,
    revisions: tuple[ReviewStatusRevision, ...],
    trunk: NativeRevision,
) -> dict[str, tuple[str, ...]]:
    """Render the `jj log` block for every revision we will print, in parallel."""

    seen: set[str] = set()
    ordered: list[NativeRevision] = []
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
    disagreements: Sequence[SubmittedStateDisagreement],
) -> tuple[tuple[ui.TableCell, ui.TableCell], ...]:
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
    rows: list[tuple[ui.TableCell, ui.TableCell]] = []
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
    change_phrase = _link_advisory_change_phrase(link_revisions)
    restart_submit_command = ui.cmd(f"jj-review submit --restart {selected_revset}")
    if states == {"closed"}:
        label = "Closed GitHub PR" if len(link_revisions) == 1 else "Closed GitHub PRs"
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
            " if branch state may be stale. Relink an open PR if one exists; otherwise run ",
            restart_submit_command,
            " to create fresh PRs.",
        )
        return label, detail
    if states == {"ambiguous"}:
        label = "Ambiguous GitHub PR" if len(link_revisions) == 1 else "Ambiguous GitHub PRs"
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


def _link_advisory_change_phrase(link_revisions: tuple[_ClassifiedStatusRevision, ...]) -> str:
    if len(link_revisions) == 1:
        return "the change shown above"
    return "one or more changes shown above"


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
    verbose: bool,
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
        bookmark=None if verbose else revision.bookmark,
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
    if change_status.link == "unlinked":
        return "submitted"
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
    cached_change = revision.cached_change
    cached_label = _format_cached_pull_request_label(cached_change)
    change_status = classified.status
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
        elif change_status.saved_review_identity:
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
        if review_decision == "unknown" and lookup.review_decision_error is not None:
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
        elif change_status.saved_review_identity:
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
            summary = f"{pr_label} merged into {lookup.pull_request.base.ref}, cleanup needed"
        else:
            summary = f"{pr_label} closed"
    else:
        message = (
            ui.plain_text(lookup.message)
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


def _classified_revision_has_link_advisory(
    classified: _ClassifiedStatusRevision,
) -> bool:
    change_status = classified.status
    if change_status.link == "unlinked":
        return False
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
        cached_change = revision.cached_change
        if cached_change is not None and cached_change.pr_number is not None:
            return (
                f"GitHub did not report remembered PR #{cached_change.pr_number} for this branch"
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
