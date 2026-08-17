"""List the stacks `jj-stack` is tracking in this local repo.

It shows one row per locally known stack, including the head change ID, stack size, PR state,
and description of the head change. It does not discover stacks that exist only on GitHub.

It also shows orphaned PRs: tracked PRs whose local change is no longer part of any current
stack. Close them and remove their branches, comments, and saved links with
`jj-stack cleanup --pull-request orphans --close`.

It reads pull request state from GitHub, but discovers stack membership from the local DAG using
local `trunk()` as the lower boundary. If your local copy of trunk is behind, `jj-stack`'s picture
of membership can be stale even though the pull request state is current. Run `jj git fetch`
first when the list needs to reflect the latest trunk.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._json_status import (
    saved_pr_json,
    stack_change_json,
)
from jj_stack.console import requested_color_mode
from jj_stack.errors import EXIT_INCOMPLETE, CliError, ErrorMessage, error_message
from jj_stack.github.resolution import (
    GithubTarget,
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.change_status import (
    ChangeStatus,
    OrphanedRecord,
    classify_stack_status_change,
    enumerate_orphaned_records,
    submitted_state_disagreement,
)
from jj_stack.stack.pr_branches import duplicate_pr_branch_claims
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.status import (
    PreparedStack,
    PRLookup,
    StackStatusChange,
    build_status_changes_for_prepared_stack,
    lookup_pr_lookups,
    observe_remote_targets_for_status,
    prepare_stack_for_status,
)

HELP = "List the stacks `jj-stack` is tracking in this repo"


@dataclass(frozen=True, slots=True)
class StackRow:
    changes: tuple[StackStatusChange, ...]
    current: bool
    current_change_ids: frozenset[str]
    head_change_id: str
    incomplete: bool
    prs: str
    size: int
    state: ui.Message
    subject: str


@dataclass(frozen=True, slots=True)
class OrphanRow:
    """One orphaned PR — its local change has left every current stack."""

    branch: str
    change_id: str
    pr: dict[str, object] | None
    pr_label: str
    state: ui.Message
    subject: str


@dataclass(frozen=True, slots=True)
class _PreparedDiscoveredStack:
    current: bool
    prepared: PreparedStack


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
    if state.pr_identities:
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

    ordered = _order_discovered_stacks(
        discovered,
        current_tracked_commit_id=current_tracked_commit_id,
    )
    duplicate_branches = duplicate_pr_branch_claims(
        (identity.head_ref, change.change_id)
        for stack in ordered
        for change in stack.changes
        if (identity := state.pr_identities.get(change.change_id)) is not None
    )
    duplicate_branch_names = frozenset(duplicate_branches)
    orphan_rows = tuple(
        _build_orphan_row(orphan) for orphan in enumerate_orphaned_records(state, ordered)
    )
    if not ordered:
        if as_json:
            console.machine_output(
                json.dumps(
                    _json_list_payload(orphan_rows=orphan_rows, rows=()),
                    indent=2,
                )
            )
            return 0
        if not orphan_rows:
            console.output("No stacks.")
            return 0
        color_when = context.jj_client.resolve_color_when(
            cli_color=requested_color_mode(),
            stdout_is_tty=sys.stdout.isatty(),
        )
        with console.spinner(description="Rendering jj change IDs"):
            rendered_change_ids = context.jj_client.render_short_change_ids(
                tuple(row.change_id for row in orphan_rows),
                color_when=color_when,
            )
        console.output(
            _stack_table(
                orphan_rows=orphan_rows,
                rendered_change_ids=rendered_change_ids,
                rows=(),
            )
        )
        _emit_orphan_hint(orphan_rows)
        return 0
    github_target = resolve_github_target(context.jj_client.list_git_remotes())
    with console.spinner(description="Inspecting PR branches"):
        observed_remote_targets = observe_remote_targets_for_status(
            context=context,
            excluded_branches=duplicate_branch_names,
            remote=github_target.remote,
            stacks=ordered,
            state=state,
        )
        prepared_discovered = tuple(
            _PreparedDiscoveredStack(
                current=_stack_contains_commit_id(
                    stack,
                    commit_id=current_tracked_commit_id,
                ),
                prepared=prepare_stack_for_status(
                    context=context,
                    observed_remote_targets=observed_remote_targets,
                    remote=github_target.remote,
                    remote_error=github_target.remote_error,
                    stack=stack,
                    state=state,
                ),
            )
            for stack in ordered
        )
    for branch, change_ids in sorted(duplicate_branches.items()):
        console.warning(
            t"PR branch {ui.bookmark(branch)} is saved for changes "
            t"{ui.join(ui.change_id, change_ids)}. Live GitHub details for those changes "
            t"were not inspected."
        )
    pr_lookups, github_error = _load_pr_lookups(
        excluded_branches=duplicate_branch_names,
        github_target=github_target,
        prepared_discovered=prepared_discovered,
    )
    rows = tuple(
        _build_row(
            github_error=github_target.github_repo_error or github_error,
            is_current=item.current,
            prepared_stack=item.prepared,
            pr_lookups=pr_lookups,
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
    color_when = context.jj_client.resolve_color_when(
        cli_color=requested_color_mode(),
        stdout_is_tty=sys.stdout.isatty(),
    )
    head_change_ids_to_render = tuple(row.head_change_id for row in rows) + tuple(
        row.change_id for row in orphan_rows
    )
    with console.spinner(description="Rendering jj change IDs"):
        rendered_change_ids = context.jj_client.render_short_change_ids(
            head_change_ids_to_render,
            color_when=color_when,
        )
    console.output(
        _stack_table(
            orphan_rows=orphan_rows,
            rendered_change_ids=rendered_change_ids,
            rows=rows,
        )
    )
    _emit_orphan_hint(orphan_rows)
    _emit_stale_stacks_advisory(discovered=ordered, state=state)
    return EXIT_INCOMPLETE if incomplete else 0


def _build_orphan_row(orphan: OrphanedRecord) -> OrphanRow:
    pr_number = orphan.pr_identity.pr_number
    return OrphanRow(
        branch=orphan.pr_identity.head_ref,
        change_id=orphan.change_id,
        pr=saved_pr_json(orphan.pr_identity),
        pr_label=f"PR #{pr_number}",
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
    payload: dict[str, object] = {
        "branch": row.branch,
        "change_id": row.change_id,
        "status": ui.plain_text(row.state),
        "subject": row.subject,
        "type": "orphan",
    }
    if row.pr is not None:
        payload["pr"] = row.pr
    return payload


def _emit_orphan_hint(orphan_rows: tuple[OrphanRow, ...]) -> None:
    if not orphan_rows:
        return
    command = ui.cmd("cleanup --pull-request orphans --close")
    console.note(t"Orphan cleanup: preview and run {command}.")


def _emit_stale_stacks_advisory(
    *,
    discovered: tuple[LocalStack, ...],
    state: TrackingState,
) -> None:
    """Hint that tracked stacks have changed since their last successful submit.

    Submitted-state disagreement means the saved baseline from the last successful
    submit no longer matches the live DAG. The right follow-up can depend on the specific
    stack state, so this advisory directs the user to inspect each stack rather
    than naming one mutation.
    """

    stale_heads = tuple(
        stack.head.change_id
        for stack in discovered
        if submitted_state_disagreement(state, (stack,))
    )
    if not stale_heads:
        return
    if len(stale_heads) == 1:
        head = stale_heads[0][:8]
        console.warning(
            (
                "Tracked stack has changed since its last submit; ",
                t"inspect with {ui.cmd(f'jj-stack view {head}')} or refresh with "
                t"{ui.cmd(f'jj-stack submit {head}')}.",
            )
        )
        return
    heads_fragments = ui.join(ui.change_id, stale_heads)
    console.warning(
        (
            "Tracked stacks have changed since their last submit; ",
            t"inspect with {ui.cmd('jj-stack view <head>')} or refresh with "
            t"{ui.cmd('jj-stack submit <head>')}: ",
            *heads_fragments,
        )
    )


def _order_discovered_stacks(
    discovered: tuple[LocalStack, ...],
    *,
    current_tracked_commit_id: str | None,
) -> tuple[LocalStack, ...]:
    return tuple(
        sorted(
            discovered,
            key=lambda stack: (
                0
                if _stack_contains_commit_id(
                    stack,
                    commit_id=current_tracked_commit_id,
                )
                else 1,
                stack.head.change_id,
            ),
        )
    )


def _stack_contains_commit_id(
    stack: LocalStack,
    *,
    commit_id: str | None,
) -> bool:
    if commit_id is None:
        return False
    return any(change.commit_id == commit_id for change in stack.changes)


def _build_row(
    *,
    github_error: ErrorMessage | None,
    is_current: bool,
    prepared_stack: PreparedStack,
    pr_lookups: dict[str, PRLookup],
) -> StackRow:
    stack = prepared_stack.stack
    changes = build_status_changes_for_prepared_stack(
        prepared_stack,
        pr_lookups=pr_lookups,
    )
    statuses = tuple(classify_stack_status_change(change) for change in changes)
    pr_numbers = _pr_numbers_from_changes(changes)
    prs = _format_pr_summary(pr_numbers)
    local_fragments: list[ui.Message] = []
    if any(change.divergent for change in stack.changes):
        local_fragments.append(ui.semantic_text("divergent", "error", "heading"))
    if any(change.conflict for change in stack.changes):
        local_fragments.append(ui.semantic_text("conflicted", "error", "heading"))
    state = _state_from_status(
        github_error=github_error,
        local_fragments=tuple(local_fragments),
        remote_error=prepared_stack.remote_error,
        changes=changes,
        statuses=statuses,
    )
    return StackRow(
        changes=changes,
        current=is_current,
        current_change_ids=frozenset(
            change.change_id for change in stack.changes if change.current_working_copy
        ),
        head_change_id=stack.head.change_id,
        incomplete=_status_is_incomplete(
            github_error=github_error,
            remote_error=prepared_stack.remote_error,
            statuses=statuses,
        ),
        prs=prs,
        size=len(stack.changes),
        state=state,
        subject=stack.head.subject,
    )


def _state_from_status(
    *,
    github_error: ErrorMessage | None,
    local_fragments: tuple[ui.Message, ...],
    remote_error: ErrorMessage | None,
    changes: tuple[StackStatusChange, ...],
    statuses: tuple[ChangeStatus, ...] | None = None,
) -> ui.Message:
    if statuses is None:
        statuses = tuple(classify_stack_status_change(change) for change in changes)
    fragments = [
        *local_fragments,
        *_status_fragments(
            github_error=github_error,
            remote_error=remote_error,
            statuses=statuses,
        ),
    ]
    if fragments:
        joined: list[ui.Message] = []
        for index, fragment in enumerate(fragments):
            if index:
                joined.append(", ")
            joined.append(fragment)
        return tuple(joined)
    if any(status.saved_pr_identity for status in statuses):
        return "tracked"
    return "not submitted"


def _status_fragments(
    *,
    github_error: ErrorMessage | None,
    remote_error: ErrorMessage | None,
    statuses: tuple[ChangeStatus, ...],
) -> tuple[ui.Message, ...]:
    fragments: list[ui.Message] = []
    if github_error is not None or remote_error is not None:
        fragments.append(ui.semantic_text("GitHub unavailable", "warning", "heading"))

    merged_ancestors = sum(1 for status in statuses if status.pr_lifecycle == "merged")
    if merged_ancestors:
        label = (
            "cleanup needed"
            if merged_ancestors == 1
            else f"{merged_ancestors} merged, cleanup needed"
        )
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    closed = sum(1 for status in statuses if status.pr_lifecycle == "closed")
    if closed:
        label = "closed" if closed == 1 else f"{closed} closed"
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    stale_links = sum(1 for status in statuses if status.has_stale_pr_link)
    if stale_links:
        label = "stale link" if stale_links == 1 else f"{stale_links} stale links"
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    ambiguous = sum(1 for status in statuses if status.pr_lifecycle == "ambiguous")
    if ambiguous:
        label = "ambiguous PR" if ambiguous == 1 else f"{ambiguous} ambiguous PRs"
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    lookup_failures = sum(1 for status in statuses if status.has_pr_lookup_failure)
    if lookup_failures:
        label = (
            "GitHub lookup failed"
            if lookup_failures == 1
            else f"{lookup_failures} GitHub lookups failed"
        )
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    queued = sum(1 for status in statuses if status.pr_queued is True)
    if queued:
        label = "queued" if queued == 1 else f"{queued} queued"
        fragments.append(ui.semantic_text(label, "hint", "heading"))

    drafts = sum(
        1 for status in statuses if status.pr_draft is True and status.pr_queued is not True
    )
    if drafts:
        label = "draft" if drafts == 1 else f"{drafts} drafts"
        fragments.append(ui.semantic_text(label, "hint", "heading"))

    open_non_draft_decisions = tuple(
        status.pr_review_decision
        for status in statuses
        if status.pr_lifecycle == "open"
        and status.pr_draft is False
        and status.pr_queued is not True
    )
    changes_requested = sum(
        1 for decision in open_non_draft_decisions if decision == "changes_requested"
    )
    if changes_requested:
        label = (
            "changes requested"
            if changes_requested == 1
            else f"{changes_requested} changes requested"
        )
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    approved = sum(1 for decision in open_non_draft_decisions if decision == "approved")
    open_neutral = sum(
        1
        for decision in open_non_draft_decisions
        if decision not in {"approved", "changes_requested"}
    )
    total_open = approved + changes_requested + drafts + open_neutral
    if approved:
        label = "approved" if approved == total_open else f"{approved} approved"
        fragments.append(ui.semantic_text(label, "hint", "heading"))
    if open_neutral:
        label = "open" if open_neutral == 1 else f"{open_neutral} open"
        fragments.append(label)
    return tuple(fragments)


def _status_is_incomplete(
    *,
    github_error: ErrorMessage | None,
    remote_error: ErrorMessage | None,
    statuses: tuple[ChangeStatus, ...],
) -> bool:
    if github_error is not None or remote_error is not None:
        return True
    return any(status.makes_report_incomplete for status in statuses)


def _pr_numbers_from_changes(
    changes: tuple[StackStatusChange, ...],
) -> tuple[int, ...]:
    numbers: list[int] = []
    for change in changes:
        lookup = change.pr_lookup
        if lookup is not None and lookup.pr is not None:
            numbers.append(lookup.pr.number)
            continue
        pr_identity = change.pr_identity
        if pr_identity is not None:
            numbers.append(pr_identity.pr_number)
    return tuple(sorted(dict.fromkeys(numbers)))


def _load_pr_lookups(
    *,
    excluded_branches: frozenset[str],
    github_target: GithubTarget | UnresolvedGithubTarget,
    prepared_discovered: tuple[_PreparedDiscoveredStack, ...],
) -> tuple[dict[str, PRLookup], ErrorMessage | None]:
    if not isinstance(github_target, GithubTarget):
        return {}, None

    prepared_changes_by_branch = {
        branch: change
        for item in prepared_discovered
        for change in item.prepared.status_changes
        if change.pr_identity is not None
        and (branch := change.branch) is not None
        and branch not in excluded_branches
    }
    if not prepared_changes_by_branch:
        return {}, None

    try:
        with console.progress(
            description="Inspecting GitHub",
            total=len(prepared_changes_by_branch),
        ) as progress:
            return (
                lookup_pr_lookups(
                    github_repo=github_target.repo,
                    on_progress=progress.advance,
                    prepared_changes=tuple(prepared_changes_by_branch.values()),
                ),
                None,
            )
    except CliError as error:
        return {}, error_message(error)


def _format_pr_summary(numbers: tuple[int, ...]) -> str:
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"PR {numbers[0]}"
    return f"{len(numbers)} PRs"


def _stack_table(
    *,
    orphan_rows: tuple[OrphanRow, ...],
    rendered_change_ids: dict[str, str],
    rows: tuple[StackRow, ...],
) -> ui.DataTable:
    stack_table_rows = [
        (
            (
                f"@ {rendered_change_ids.get(row.head_change_id, row.head_change_id[:8])}"
                if row.current
                else rendered_change_ids.get(row.head_change_id, row.head_change_id[:8])
            ),
            f"{row.size} {'change' if row.size == 1 else 'changes'}",
            row.prs,
            row.state,
            row.subject,
        )
        for row in rows
    ]
    for orphan in orphan_rows:
        stack_table_rows.append(
            (
                rendered_change_ids.get(orphan.change_id, orphan.change_id[:8]),
                "orphan",
                orphan.pr_label,
                orphan.state,
                orphan.subject,
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
        pad_edge=False,
        padding=(0, 0),
        show_edge=False,
        rows=tuple(stack_table_rows),
    )
