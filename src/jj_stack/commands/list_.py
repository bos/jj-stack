"""List the stacks `jj-stack` is tracking in this local repo.

Each row shows the head change ID, stack size, PR state, and head description. Stacks without
any submitted changes and stacks that exist only on GitHub are not listed.

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

import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
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
from jj_stack.identifiers import short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.stack.change_state import (
    ChangeObservation,
    ChangeState,
    OrphanedRecord,
    enumerate_orphaned_records,
)
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.pr_branches import duplicate_pr_branch_claims
from jj_stack.stack.preparation import PreparedLocalStack
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.reporting import report_change, status_label, submittable_edits
from jj_stack.stack.status import StackStatusChange, build_status_result, observe_status

HELP = "List the stacks jj-stack is tracking in this repo"


@dataclass(frozen=True, slots=True)
class StackRow:
    changes: tuple[StackStatusChange, ...]
    current: bool
    current_change_ids: frozenset[str]
    head_change_id: str
    incomplete: bool
    prs: ui.Message
    size: int
    state: ui.Message
    subject: str


@dataclass(frozen=True, slots=True)
class OrphanRow:
    """One orphaned PR — its local change has left every current stack."""

    branch: str
    change_id: str
    pr: dict[str, object]
    pr_label: ui.Message
    state: ui.Message
    subject: str


@dataclass(frozen=True, slots=True)
class _PreparedDiscoveredStack:
    current: bool
    prepared: PreparedLocalStack


def list_(
    *,
    as_json: bool,
    cli_args: JjCliArgs,
    debug: bool,
    repo: Path | None,
) -> int:
    """CLI entrypoint for `list`."""

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
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
    ordered = tuple(
        sorted(
            discovered,
            key=lambda stack: (
                0
                if current_tracked_commit_id is not None
                and any(change.commit_id == current_tracked_commit_id for change in stack.changes)
                else 1,
                stack.head.change_id,
            ),
        )
    )
    duplicate_branches = duplicate_pr_branch_claims(
        (tracked.pr_identity.head_ref, change.change_id)
        for stack in ordered
        for change in stack.changes
        if (tracked := state.prs.get(change.change_id)) is not None
    )
    duplicate_branch_names = frozenset(duplicate_branches)
    orphan_rows = tuple(
        _build_orphan_row(orphan, repo=github_repo)
        for orphan in enumerate_orphaned_records(state, ordered)
    )
    if not ordered and not orphan_rows and not as_json:
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
        prepared_discovered = tuple(
            _PreparedDiscoveredStack(
                current=current_tracked_commit_id is not None
                and any(
                    change.commit_id == current_tracked_commit_id for change in stack.changes
                ),
                prepared=PreparedLocalStack(
                    client=context.jj_client,
                    github_target=github_target,
                    stack=stack,
                    state=state,
                ),
            )
            for stack in ordered
        )
        with console.spinner(description="Inspecting GitHub"):
            lookups = observe_status(
                prepared=tuple(item.prepared for item in prepared_discovered),
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
                is_current=item.current,
                prepared_stack=item.prepared,
                pr_lookups=lookups,
            )
            for item in prepared_discovered
        )
    incomplete = bool(duplicate_branches) or any(row.incomplete for row in rows)
    if as_json:
        console.machine_output(
            json.dumps(
                _json_list_payload(orphan_rows=orphan_rows, rows=rows),
                indent=2,
            )
        )
        return EXIT_INCOMPLETE if incomplete else 0
    jj_color = color_when(stdout_is_tty=sys.stdout.isatty())
    with console.spinner(description="Rendering jj change IDs"):
        rendered_change_ids = context.jj_client.render_short_change_ids(
            (*(row.head_change_id for row in rows), *(row.change_id for row in orphan_rows)),
            color_when=jj_color,
        )
    console.output(
        _stack_table(
            orphan_rows=orphan_rows,
            rendered_change_ids=rendered_change_ids,
            rows=rows,
        )
    )
    _emit_orphan_hint(orphan_rows)
    _emit_divergence_hints(rows)
    _emit_stale_stacks_advisory(rows)
    return EXIT_INCOMPLETE if incomplete else 0


def _build_orphan_row(
    orphan: OrphanedRecord,
    *,
    repo: GithubRepoAddress | None,
) -> OrphanRow:
    pr_number = orphan.pr_identity.pr_number
    return OrphanRow(
        branch=orphan.pr_identity.head_ref,
        change_id=orphan.change_id,
        pr=saved_pr_json(orphan.pr_identity),
        pr_label=format_pr_label(pr_number, repo=repo),
        state=ui.semantic_text("orphan", "warning", "heading"),
        subject="local change missing",
    )


def _json_list_payload(
    *,
    orphan_rows: tuple[OrphanRow, ...],
    rows: tuple[StackRow, ...],
) -> dict[str, object]:
    return {
        "rows": [
            *(_json_stack_row(row) for row in rows),
            *(_json_orphan_row(row) for row in orphan_rows),
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


def _json_orphan_row(row: OrphanRow) -> dict[str, object]:
    return {
        "branch": row.branch,
        "change_id": row.change_id,
        "pr": row.pr,
        "status": ui.plain_text(row.state),
        "subject": row.subject,
        "type": "orphan",
    }


def _emit_orphan_hint(orphan_rows: tuple[OrphanRow, ...]) -> None:
    if not orphan_rows:
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
    local_fragments: list[ui.Message] = []
    if any(change.conflict for change in stack.changes):
        local_fragments.append(ui.semantic_text("conflicted", "error", "heading"))
    state = _state_from_status(
        github_error=result.github_error,
        local_fragments=tuple(local_fragments),
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
        prs=_format_pr_summary(changes, repo=github_repo),
        size=len(stack.changes),
        state=state,
        subject=stack.head.subject,
    )


def _state_from_status(
    *,
    github_error: ErrorMessage | None,
    local_fragments: tuple[ui.Message, ...],
    remote_error: ErrorMessage | None,
    states: tuple[ChangeState, ...],
) -> ui.Message:
    fragments = [
        *local_fragments,
        *_status_fragments(
            github_error=github_error,
            remote_error=remote_error,
            states=states,
        ),
    ]
    if fragments:
        joined: list[ui.Message] = []
        for index, fragment in enumerate(fragments):
            if index:
                joined.append(", ")
            joined.append(fragment)
        return tuple(joined)
    if any(state.tracked is not None for state in states):
        return "tracked"
    return "not submitted"


def _status_fragments(
    *,
    github_error: ErrorMessage | None,
    remote_error: ErrorMessage | None,
    states: tuple[ChangeState, ...],
) -> tuple[ui.Message, ...]:
    fragments: list[ui.Message] = []
    if github_error is not None or remote_error is not None:
        fragments.append(ui.semantic_text("GitHub unavailable", "warning", "heading"))

    reports = tuple(report_change(state) for state in states)
    counts = Counter(report.status for report in reports)
    # Unsubmitted changes have their own local stack rows.
    for status, count in counts.items():
        if status not in {"unsubmitted", "submitted"}:
            label = status_label(status, count=count)
            if status == "approved" and count == 1 and len(reports) > 1:
                label = t"1 {label}"
            fragments.append(label)

    check_statuses = {report.checks for report in reports if report.problem is None}
    for rollup_status, labels in (
        ("failed", ("warning", "heading")),
        ("pending", ("hint", "heading")),
        ("passed", ("hint", "heading")),
    ):
        if rollup_status in check_statuses:
            fragments.append(ui.semantic_text(f"checks {rollup_status}", *labels))
            break
    return tuple(fragments)


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


def _format_pr_summary(
    changes: tuple[StackStatusChange, ...],
    *,
    repo: GithubRepoAddress | None,
) -> ui.Message:
    references = _pr_references_from_changes(changes)
    if not references:
        return ""
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
    orphan_rows: tuple[OrphanRow, ...],
    rendered_change_ids: dict[str, str],
    rows: tuple[StackRow, ...],
) -> ui.DataTable:
    stack_table_rows = [
        (
            (
                f"@ {
                    rendered_change_ids.get(
                        row.head_change_id,
                        short_change_id(row.head_change_id),
                    )
                }"
                if row.current
                else rendered_change_ids.get(
                    row.head_change_id,
                    short_change_id(row.head_change_id),
                )
            ),
            f"{row.size} {'change' if row.size == 1 else 'changes'}",
            row.prs,
            row.state,
            t"{row.subject}",
        )
        for row in rows
    ]
    for orphan in orphan_rows:
        stack_table_rows.append(
            (
                rendered_change_ids.get(orphan.change_id, short_change_id(orphan.change_id)),
                "orphan",
                orphan.pr_label,
                orphan.state,
                t"{orphan.subject}",
            )
        )
    return ui.DataTable(
        columns=(
            ui.TableColumn("head", no_wrap=True),
            ui.TableColumn("size", no_wrap=True),
            ui.TableColumn("PRs", no_wrap=True),
            ui.TableColumn("state"),
            ui.TableColumn("description"),
        ),
        padding=(0, 0),
        rows=tuple(stack_table_rows),
    )
