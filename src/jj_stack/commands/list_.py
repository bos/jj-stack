"""List the stacks `jj-stack` is tracking in this local repo.

Each row shows the head change ID, the stack's PRs, PR state, and head description. The change
count appears too when the stack has changes without PRs. Stacks without any submitted changes
and stacks that exist only on GitHub are not listed.

Orphaned PRs are listed separately: their local changes are no longer in any stack. The orphan
rows show saved PR links without checking their current GitHub state. To close them and remove
their unused branches, stack overview comments, and saved links, use
`jj-stack cleanup --pull-request orphans --close`.

For local stacks, PR state comes from GitHub and stack order comes from local history. This
command does not fetch. Run `jj git fetch` first if you need to update local `trunk()`.

In terminals with hyperlink support, click the PR label in a row to open it on GitHub. A count
such as `5 PRs` links to the topmost PR in that stack.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, replace

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, GlobalOptions, bootstrap_context
from jj_stack.commands._json_status import (
    saved_pr_json,
    stack_change_json,
)
from jj_stack.console import color_when
from jj_stack.errors import EXIT_INCOMPLETE, CliError, ErrorMessage, error_message
from jj_stack.formatting import format_pr_label, pr_url
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.identifiers import ChangeId, short_change_id
from jj_stack.stack.change_state import (
    ChangeObservation,
    ChangeState,
    OrphanedRecord,
    WithPR,
    enumerate_orphaned_records,
)
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.pr_branches import duplicate_pr_branch_claims
from jj_stack.stack.preparation import PreparedLocalStack
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.reporting import (
    ChangeReport,
    report_change,
    stack_behind,
    status_label,
    submittable_edits,
)
from jj_stack.stack.status import StackStatusChange, build_status_result, observe_status

HELP = "List the stacks jj-stack is tracking in this repo"


@dataclass(frozen=True, slots=True)
class StackRow:
    changes: tuple[StackStatusChange, ...]
    current: bool
    current_change_ids: frozenset[ChangeId]
    head_change_id: ChangeId
    incomplete: bool
    size: ui.Message
    state: ui.Message
    subject: str


# Orphaned PRs: their local change has left every current stack.
_ORPHAN_STATE = ui.semantic_text("orphan", "warning", "heading")
_ORPHAN_SUBJECT = "local change missing"


def list_(
    *,
    global_options: GlobalOptions,
    as_json: bool,
    ignore_working_copy: bool,
) -> int:
    """CLI entrypoint for `list`."""

    context = bootstrap_context(global_options, snapshot_working_copy=not ignore_working_copy)
    return _run_list(
        as_json=as_json,
        context=context,
    )


def _run_list(
    *,
    as_json: bool,
    context: CommandContext,
) -> int:
    state = context.state_store.load()
    if state.prs:
        with console.spinner(description="Inspecting local stacks"):
            repo_paths = observe_repo_paths(
                jj_client=context.jj_client,
                state=state,
            )
        discovered = tuple(path.stack for path in repo_paths.paths if path.tracked_change_ids)
        current_tracked_commit_id = repo_paths.current_tracked_commit_id
    else:
        discovered = ()
        current_tracked_commit_id = None

    github_target = (
        resolve_github_target(context.jj_client.list_git_remotes())
        if state.prs
        else UnresolvedGithubTarget()
    )
    github_repo = github_target.repo if isinstance(github_target, GithubTarget) else None
    current_heads = frozenset(
        stack.head.commit_id
        for stack in discovered
        if any(change.commit_id == current_tracked_commit_id for change in stack.changes)
    )
    ordered = tuple(
        sorted(
            discovered,
            key=lambda stack: (stack.head.commit_id not in current_heads, stack.head.change_id),
        )
    )
    duplicate_branches = duplicate_pr_branch_claims(
        (tracked.pr_identity.head_ref, change.change_id)
        for stack in ordered
        for change in stack.changes
        if (tracked := state.prs.get(change.change_id)) is not None
    )
    duplicate_branch_names = frozenset(duplicate_branches)
    orphans = enumerate_orphaned_records(state, ordered)
    if not ordered and not orphans and not as_json:
        console.output("No stacks.")
        return 0
    rows: tuple[StackRow, ...] = ()
    if ordered:
        for branch, change_ids in sorted(duplicate_branches.items()):
            console.warning(
                t"PR branch {ui.bookmark(branch)} is saved for changes "
                t"{ui.join(ui.change_id, change_ids)}. Live GitHub details for those changes "
                t"were not inspected."
            )
        prepared_stacks = tuple(
            PreparedLocalStack(
                client=context.jj_client,
                github_target=github_target,
                stack=stack,
                state=state,
            )
            for stack in ordered
        )
        with console.spinner(description="Inspecting GitHub"):
            lookups = observe_status(
                context=context,
                prepared=prepared_stacks,
                exclude_branches=duplicate_branch_names,
            )
        github_error = error_message(lookups) if isinstance(lookups, CliError) else None
        for message in remote_and_github_unavailable_messages(
            github_error=github_target.github_repo_error or github_error,
            github_repo=github_repo,
            remote=github_target.remote,
            remote_error=github_target.remote_error,
        ):
            console.warning(message, soft_wrap=False)
        rows = tuple(
            _build_row(
                github_repo=github_repo,
                is_current=prepared.stack.head.commit_id in current_heads,
                prepared_stack=prepared,
                pr_lookups=lookups,
            )
            for prepared in prepared_stacks
        )
    incomplete = bool(duplicate_branches) or any(row.incomplete for row in rows)
    if as_json:
        console.machine_output(_json_list_payload(orphans=orphans, rows=rows))
        return EXIT_INCOMPLETE if incomplete else 0
    jj_color = color_when(stdout_is_tty=sys.stdout.isatty())
    with console.spinner(description="Rendering jj change IDs"):
        rendered_change_ids = context.jj_client.render_short_change_ids(
            (*(row.head_change_id for row in rows), *(orphan.change_id for orphan in orphans)),
            color_when=jj_color,
        )
    console.output(
        _stack_table(
            github_repo=github_repo,
            orphans=orphans,
            rendered_change_ids=rendered_change_ids,
            rows=rows,
        )
    )
    _emit_orphan_hint(orphans)
    _emit_divergence_hints(rows)
    _emit_stale_stacks_advisory(rows)
    return EXIT_INCOMPLETE if incomplete else 0


def _json_list_payload(
    *,
    orphans: tuple[OrphanedRecord, ...],
    rows: tuple[StackRow, ...],
) -> dict[str, object]:
    return {
        "rows": [
            *(_json_stack_row(row) for row in rows),
            *(_json_orphan_row(orphan) for orphan in orphans),
        ],
    }


def _json_stack_row(row: StackRow) -> dict[str, object]:
    payload: dict[str, object] = {
        "head_change_id": row.head_change_id,
        "changes": [
            stack_change_json(
                change,
                current=change.change_id in row.current_change_ids,
            )
            for change in row.changes
        ],
        "status": ui.plain_text(row.state),
        "subject": row.subject,
        "type": "stack",
    }
    if row.current:
        payload["current"] = True
    return payload


def _json_orphan_row(orphan: OrphanedRecord) -> dict[str, object]:
    return {
        "branch": orphan.pr_identity.head_ref,
        "change_id": orphan.change_id,
        "pr": saved_pr_json(orphan.pr_identity),
        "status": ui.plain_text(_ORPHAN_STATE),
        "subject": _ORPHAN_SUBJECT,
        "type": "orphan",
    }


def _emit_orphan_hint(orphans: tuple[OrphanedRecord, ...]) -> None:
    if not orphans:
        return
    command = ui.cmd("jj-stack cleanup --pull-request orphans --close")
    console.note(t"To close orphaned PRs and clean up, run {command}; add --dry-run to preview.")


def _emit_divergence_hints(rows: tuple[StackRow, ...]) -> None:
    change_ids = tuple(
        dict.fromkeys(
            change.change_id
            for row in rows
            for change in row.changes
            if report_change(change.state).divergent
        )
    )
    for change_id in change_ids:
        console.note(
            t"Divergent change {ui.change_id(change_id)}: {divergence_recovery_hint(change_id)}"
        )


def _emit_stale_stacks_advisory(rows: tuple[StackRow, ...]) -> None:
    stale_heads = tuple(
        row.head_change_id
        for row in rows
        if submittable_edits(
            {change.change_id: report_change(change.state) for change in row.changes}
        )
    )
    if not stale_heads:
        return
    if len(stale_heads) == 1:
        head = short_change_id(stale_heads[0])
        console.warning(
            (
                "Tracked stack has changed since its last submit; ",
                t"inspect with {ui.cmd(f'jj-stack view {head}')}.",
            )
        )
        return
    heads_fragments = ui.join(ui.change_id, stale_heads)
    console.warning(
        (
            "Tracked stacks have changed since their last submit; ",
            t"inspect with {ui.cmd('jj-stack view <head>')}: ",
            *heads_fragments,
        )
    )


def _build_row(
    *,
    github_repo: GithubRepoAddress | None,
    is_current: bool,
    prepared_stack: PreparedLocalStack,
    pr_lookups: dict[str, ChangeObservation] | CliError,
) -> StackRow:
    stack = prepared_stack.stack
    result = build_status_result(prepared=prepared_stack, pr_lookups=pr_lookups)
    # The JSON contract lists a row's changes from the bottom up.
    changes = result.changes[::-1]
    state = _state_from_status(
        conflicted=any(change.conflict for change in stack.changes),
        github_error=result.github_error,
        remote_error=result.remote_error,
        states=tuple(change.state for change in changes),
    )
    return StackRow(
        changes=changes,
        current=is_current,
        current_change_ids=frozenset(
            change.change_id for change in stack.changes if change.current_working_copy
        ),
        head_change_id=stack.head.change_id,
        incomplete=result.incomplete,
        size=_format_size(len(stack.changes), changes, repo=github_repo),
        state=state,
        subject=stack.head.subject,
    )


def _state_from_status(
    *,
    conflicted: bool,
    github_error: ErrorMessage | None,
    remote_error: ErrorMessage | None,
    states: tuple[ChangeState, ...],
) -> ui.Message:
    fragments = (
        *((ui.semantic_text("conflicted", "error", "heading"),) if conflicted else ()),
        *_status_fragments(
            github_error=github_error,
            remote_error=remote_error,
            states=states,
        ),
    )
    return ui.join(lambda fragment: fragment, fragments) if fragments else "tracked"


def _status_fragments(
    *,
    github_error: ErrorMessage | None,
    remote_error: ErrorMessage | None,
    states: tuple[ChangeState, ...],
) -> tuple[ui.Message, ...]:
    fragments: list[ui.Message] = []
    if github_error is not None or remote_error is not None:
        fragments.append(ui.semantic_text("GitHub unavailable", "warning", "heading"))

    # Changes run from the bottom up, so each fragment links the lowest PR it applies to.
    entries = tuple(
        (report_change(state), state.pr.html_url if isinstance(state, WithPR) else None)
        for state in states
    )
    reports = tuple(report for report, _url in entries)
    judged = [report.ready for report in reports if report.ready is not None]
    if judged:
        fragments.append(f"{sum(judged)} ready")
    if (behind := stack_behind(states)) is not None:
        count, branch = behind
        fragments.append(t"{count} behind {ui.bookmark(branch)}")
    counts = Counter(report.status for report in reports)
    # Unsubmitted changes have their own local stack rows; readiness covers open and approved.
    for status, count in counts.items():
        if status not in {"unsubmitted", "submitted", "open", "approved"}:
            url = next(url for report, url in entries if report.status == status)
            fragments.append(_linked(status_label(status, count=count), url))
    fragments.extend(_check_and_warning_fragments(entries))
    return tuple(fragments)


def _check_and_warning_fragments(
    entries: tuple[tuple[ChangeReport, str | None], ...],
) -> tuple[ui.Message, ...]:
    fragments: list[ui.Message] = []
    healthy = tuple((report, url) for report, url in entries if report.problem is None)
    for rollup_status, labels in (
        ("failed", ("warning", "heading")),
        ("pending", ("hint", "heading")),
    ):
        urls = [url for report, url in healthy if report.checks == rollup_status]
        if urls:
            label = ui.semantic_text(f"checks {rollup_status}", *labels)
            fragments.append(_linked(label, urls[0]))
            break
    warnings: dict[str, str | None] = {}
    for report, url in healthy:
        for warning in report.merge_warnings:
            warnings.setdefault(warning, url)
    fragments.extend(
        _linked(ui.semantic_text(warning, "warning", "heading"), url)
        for warning, url in warnings.items()
    )
    return tuple(fragments)


def _linked(label: str | ui.SemanticText, url: str | None) -> ui.Message:
    if url is None:
        return label
    if isinstance(label, ui.SemanticText):
        return replace(label, link=url)
    return ui.hyperlink(label, url)


def _pr_references_from_changes(
    changes: tuple[StackStatusChange, ...],
) -> tuple[tuple[int, str | None], ...]:
    references: dict[int, str | None] = {}
    for change in changes:
        pr = change.pr
        if pr is not None:
            references[pr.number] = pr.html_url
            continue
        if change.tracked is not None:
            references.setdefault(change.tracked.pr_identity.pr_number, None)
    return tuple(references.items())


def _format_size(
    size: int,
    changes: tuple[StackStatusChange, ...],
    *,
    repo: GithubRepoAddress | None,
) -> ui.Message:
    """Count the changes only when they are not one PR each."""

    references = _pr_references_from_changes(changes)
    counted = f"{size} {'change' if size == 1 else 'changes'}"
    if not references:
        return counted
    prs = _format_pr_summary(references, repo=repo)
    return prs if len(references) == size else (f"{counted}, ", prs)


def _format_pr_summary(
    references: tuple[tuple[int, str | None], ...],
    *,
    repo: GithubRepoAddress | None,
) -> ui.Message:
    if len(references) == 1:
        number, url = references[0]
        return format_pr_label(
            number,
            include_hash=False,
            repo=repo,
            url=url,
        )
    number, url = references[-1]
    url = pr_url(number, repo=repo, url=url)
    text = f"{len(references)} PRs"
    return ui.hyperlink(text, url) if url is not None else text


def _stack_table(
    *,
    github_repo: GithubRepoAddress | None,
    orphans: tuple[OrphanedRecord, ...],
    rendered_change_ids: dict[ChangeId, str],
    rows: tuple[StackRow, ...],
) -> ui.DataTable:
    stack_table_rows = [
        (
            f"{'@ ' if row.current else ''}{rendered_change_ids[row.head_change_id]}",
            row.size,
            row.state,
            t"{row.subject}",
        )
        for row in rows
    ]
    for orphan in orphans:
        stack_table_rows.append(
            (
                rendered_change_ids[orphan.change_id],
                format_pr_label(orphan.pr_identity.pr_number, repo=github_repo),
                _ORPHAN_STATE,
                t"{_ORPHAN_SUBJECT}",
            )
        )
    return ui.DataTable(
        columns=(
            ui.TableColumn("head", no_wrap=True),
            ui.TableColumn("size", no_wrap=True),
            ui.TableColumn("state"),
            ui.TableColumn("description"),
        ),
        rows=tuple(stack_table_rows),
    )
