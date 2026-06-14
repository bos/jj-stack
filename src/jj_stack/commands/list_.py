"""List stacks in this repository.

Shows one row per stack in this repo, including the head change ID, stack size, review
state, and description of the head commit.

It also shows orphaned PRs: open PRs that `jj-stack` still knows about, but whose local change
is no longer part of any current stack. Close those explicitly with
`jj-stack unstack --cleanup --pull-request <pr>`.

`--fetch` runs a fetch first so the report uses current remote branch locations.
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
    cached_pull_request_json,
    review_change_json,
)
from jj_stack.commands._stale_stacks import emit_stale_stacks_advisory
from jj_stack.console import requested_color_mode
from jj_stack.errors import CliError, ErrorMessage, error_message
from jj_stack.github.resolution import (
    GithubTarget,
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.change_status import (
    OrphanedRecord,
    ReviewChangeStatus,
    classify_review_status_revision,
    classify_saved_review_change,
    enumerate_orphaned_records,
)
from jj_stack.review.discovery import discover_tracked_stacks
from jj_stack.review.status import (
    PreparedRevision,
    PreparedStack,
    PullRequestLookup,
    ReviewStatusRevision,
    build_status_revisions_for_prepared_stack,
    lookup_pull_request_lookups,
    pinned_bookmarks_for_revisions,
    prepare_stack_for_status,
    refresh_remote_state_for_status,
)

HELP = "List stacks in this repo"


@dataclass(frozen=True, slots=True)
class StackRow:
    changes: tuple[ReviewStatusRevision, ...]
    current: bool
    current_change_ids: frozenset[str]
    head_change_id: str
    incomplete: bool
    review: str
    size: int
    state: ui.Message
    subject: str


@dataclass(frozen=True, slots=True)
class OrphanRow:
    """One orphaned PR — its local change has left every current stack."""

    bookmark: str | None
    change_id: str
    hint: str | None
    pull_request: dict[str, object] | None
    review: str
    state: ui.Message
    subject: str


@dataclass(frozen=True, slots=True)
class _PreparedDiscoveredStack:
    current: bool
    prepared: PreparedStack


@dataclass(frozen=True, slots=True)
class _RepoInspectionContext:
    bookmark_states: dict[str, BookmarkState]
    github_target: GithubTarget | UnresolvedGithubTarget


def list_(
    *,
    as_json: bool,
    cli_args: JjCliArgs,
    debug: bool,
    fetch: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `list`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    return _run_list(
        as_json=as_json,
        context=context,
        fetch=fetch,
    )


def _run_list(
    *,
    as_json: bool,
    context: CommandContext,
    fetch: bool,
) -> int:
    if fetch:
        refresh_remote_state_for_status(jj_client=context.jj_client)

    state = context.state_store.load()
    with console.spinner(description="Inspecting local stacks"):
        discovered = discover_tracked_stacks(jj_client=context.jj_client, state=state)

    ordered = _order_discovered_stacks(
        discovered.stacks,
        current_commit_id=discovered.current_commit_id,
        jj_client=context.jj_client,
    )
    orphan_rows = tuple(
        _build_orphan_row(orphan) for orphan in enumerate_orphaned_records(state, ordered)
    )
    if not ordered:
        if as_json:
            console.output(
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
        _emit_orphan_hints(orphan_rows)
        return 0
    with console.spinner(description="Loading bookmark state"):
        repo_inspection = _prepare_repo_inspection_context(
            context=context,
            discovered=ordered,
            state=state,
        )
    github_target = repo_inspection.github_target
    prepared_discovered = tuple(
        _PreparedDiscoveredStack(
            current=_stack_contains_commit_id(
                stack,
                commit_id=discovered.current_commit_id,
            ),
            prepared=prepare_stack_for_status(
                bookmark_states=repo_inspection.bookmark_states,
                context=context,
                persist_bookmarks=False,
                remote=github_target.remote,
                remote_error=github_target.remote_error,
                stack=stack,
                state=state,
            ),
        )
        for stack in ordered
    )
    _ensure_unique_repo_bookmarks(prepared_discovered)
    pull_request_lookups, github_error = _load_pull_request_lookups(
        github_target=github_target,
        prepared_discovered=prepared_discovered,
    )
    rows = tuple(
        _build_row(
            github_error=github_target.github_repository_error or github_error,
            is_current=item.current,
            prepared_stack=item.prepared,
            pull_request_lookups=pull_request_lookups,
        )
        for item in prepared_discovered
    )
    if as_json:
        console.output(
            json.dumps(
                _json_list_payload(orphan_rows=orphan_rows, rows=rows),
                indent=2,
            )
        )
        return 1 if any(row.incomplete for row in rows) else 0
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
    _emit_orphan_hints(orphan_rows)
    _emit_stale_stacks_advisory(discovered=ordered, state=state)
    return 1 if any(row.incomplete for row in rows) else 0


def _build_orphan_row(orphan: OrphanedRecord) -> OrphanRow:
    pr_number = orphan.cached_change.pr_number
    return OrphanRow(
        bookmark=orphan.cached_change.bookmark,
        change_id=orphan.change_id,
        hint=(f"close --cleanup --pull-request {pr_number}" if pr_number is not None else None),
        pull_request=cached_pull_request_json(orphan.cached_change),
        review=f"PR #{pr_number}" if pr_number is not None else "(no PR number)",
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
            review_change_json(
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
        "change_id": row.change_id,
        "status": ui.plain_text(row.state),
        "subject": row.subject,
        "type": "orphan",
    }
    if row.bookmark is not None:
        payload["bookmark"] = row.bookmark
    if row.pull_request is not None:
        payload["pull_request"] = row.pull_request
    return payload


def _emit_orphan_hints(orphan_rows: tuple[OrphanRow, ...]) -> None:
    for orphan in orphan_rows:
        if orphan.hint is None:
            continue
        console.note(t"Orphan {orphan.review}: run {ui.cmd(orphan.hint)} to retire it.")


def _emit_stale_stacks_advisory(
    *,
    discovered: tuple[LocalStack, ...],
    state: ReviewState,
) -> None:
    """Hint that tracked stacks have changed since their last successful submit.

    Submitted-state disagreement means the saved commit or topology baseline no
    longer matches the live DAG. The right follow-up can depend on the specific
    stack state, so this advisory directs the user to inspect each stack rather
    than naming one mutation.
    """

    emit_stale_stacks_advisory(
        stacks=discovered,
        state=state,
        single_subject="Tracked stack",
        plural_subject="Tracked stacks",
    )


def _prepare_repo_inspection_context(
    *,
    context: CommandContext,
    discovered: tuple[LocalStack, ...],
    state: ReviewState,
) -> _RepoInspectionContext:
    config = context.config
    jj_client = context.jj_client
    github_target = resolve_github_target(jj_client.list_git_remotes())

    all_revisions = tuple(revision for stack in discovered for revision in stack.revisions)
    bookmark_states: dict[str, BookmarkState] = {}
    if github_target.remote is not None or config.use_bookmarks:
        pinned_bookmarks = _tracked_pinned_bookmarks_for_repo_inspection(
            revisions=all_revisions,
            state=state,
        )
        bookmark_states = jj_client.list_bookmark_states(pinned_bookmarks)

    return _RepoInspectionContext(
        bookmark_states=bookmark_states,
        github_target=github_target,
    )


def _order_discovered_stacks(
    discovered: tuple[LocalStack, ...],
    *,
    current_commit_id: str | None,
    jj_client: JjClient,
) -> tuple[LocalStack, ...]:
    head_commit_ids = tuple(stack.head.commit_id for stack in discovered)
    if not head_commit_ids:
        return ()
    ordered_heads = jj_client.query_revisions_by_commit_ids(head_commit_ids)
    order_index = {revision.commit_id: index for index, revision in enumerate(ordered_heads)}
    return tuple(
        sorted(
            discovered,
            key=lambda stack: (
                0 if _stack_contains_commit_id(stack, commit_id=current_commit_id) else 1,
                order_index.get(stack.head.commit_id, len(order_index)),
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
    return any(revision.commit_id == commit_id for revision in stack.revisions)


def _build_row(
    *,
    github_error: ErrorMessage | None,
    is_current: bool,
    prepared_stack: PreparedStack,
    pull_request_lookups: dict[str, PullRequestLookup],
) -> StackRow:
    stack = prepared_stack.stack
    revisions = build_status_revisions_for_prepared_stack(
        prepared_stack,
        pull_request_lookups=pull_request_lookups,
    )
    statuses = tuple(classify_review_status_revision(revision) for revision in revisions)
    pull_request_numbers = _pull_request_numbers_from_revisions(revisions)
    review = _format_pull_request_range(pull_request_numbers)
    local_fragments: list[ui.Message] = []
    if any(revision.divergent for revision in stack.revisions):
        local_fragments.append(ui.semantic_text("divergent", "error", "heading"))
    if any(revision.conflict for revision in stack.revisions):
        local_fragments.append(ui.semantic_text("conflicted", "error", "heading"))
    state = _state_from_status(
        github_error=github_error,
        local_fragments=tuple(local_fragments),
        remote_error=prepared_stack.remote_error,
        revisions=revisions,
        statuses=statuses,
    )
    return StackRow(
        changes=revisions,
        current=is_current,
        current_change_ids=frozenset(
            revision.change_id for revision in stack.revisions if revision.current_working_copy
        ),
        head_change_id=stack.head.change_id,
        incomplete=_status_is_incomplete(
            github_error=github_error,
            remote_error=prepared_stack.remote_error,
            statuses=statuses,
        ),
        review=review,
        size=len(stack.revisions),
        state=state,
        subject=stack.head.subject,
    )


def _state_from_status(
    *,
    github_error: ErrorMessage | None,
    local_fragments: tuple[ui.Message, ...],
    remote_error: ErrorMessage | None,
    revisions: tuple[ReviewStatusRevision, ...],
    statuses: tuple[ReviewChangeStatus, ...] | None = None,
) -> ui.Message:
    if statuses is None:
        statuses = tuple(classify_review_status_revision(revision) for revision in revisions)
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
    if any(status.saved_review_identity for status in statuses):
        return "tracked"
    return "not submitted"


def _status_fragments(
    *,
    github_error: ErrorMessage | None,
    remote_error: ErrorMessage | None,
    statuses: tuple[ReviewChangeStatus, ...],
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

    unlinked = sum(1 for status in statuses if status.link == "unlinked")
    if unlinked:
        label = "unlinked" if unlinked == 1 else f"{unlinked} unlinked"
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    closed = sum(1 for status in statuses if status.pr_lifecycle == "closed")
    if closed:
        label = "closed" if closed == 1 else f"{closed} closed"
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    stale_links = sum(1 for status in statuses if status.has_stale_pull_request_link)
    if stale_links:
        label = "stale link" if stale_links == 1 else f"{stale_links} stale links"
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    ambiguous = sum(1 for status in statuses if status.pr_lifecycle == "ambiguous")
    if ambiguous:
        label = "ambiguous PR" if ambiguous == 1 else f"{ambiguous} ambiguous PRs"
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    lookup_failures = sum(1 for status in statuses if status.has_pull_request_lookup_failure)
    if lookup_failures:
        label = (
            "GitHub lookup failed"
            if lookup_failures == 1
            else f"{lookup_failures} GitHub lookups failed"
        )
        fragments.append(ui.semantic_text(label, "warning", "heading"))

    drafts = sum(1 for status in statuses if status.pr_draft is True)
    if drafts:
        label = "draft" if drafts == 1 else f"{drafts} drafts"
        fragments.append(ui.semantic_text(label, "hint", "heading"))

    open_non_draft_decisions = tuple(
        status.pr_review_decision
        for status in statuses
        if status.pr_lifecycle == "open" and status.pr_draft is False
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
    statuses: tuple[ReviewChangeStatus, ...],
) -> bool:
    if github_error is not None or remote_error is not None:
        return True
    return any(
        status.has_stale_pull_request_link
        or status.has_pull_request_lookup_failure
        or status.pr_lifecycle == "ambiguous"
        for status in statuses
    )


def _pull_request_numbers_from_revisions(
    revisions: tuple[ReviewStatusRevision, ...],
) -> tuple[int, ...]:
    numbers: list[int] = []
    for revision in revisions:
        lookup = revision.pull_request_lookup
        if lookup is not None and lookup.pull_request is not None:
            numbers.append(lookup.pull_request.number)
            continue
        cached_change = revision.cached_change
        if cached_change is not None and cached_change.pr_number is not None:
            numbers.append(cached_change.pr_number)
    return tuple(sorted(dict.fromkeys(numbers)))


def _load_pull_request_lookups(
    *,
    github_target: GithubTarget | UnresolvedGithubTarget,
    prepared_discovered: tuple[_PreparedDiscoveredStack, ...],
) -> tuple[dict[str, PullRequestLookup], ErrorMessage | None]:
    if not isinstance(github_target, GithubTarget):
        return {}, None

    prepared_revisions_by_bookmark = _tracked_prepared_revisions_by_bookmark(
        prepared_discovered=prepared_discovered
    )
    if not prepared_revisions_by_bookmark:
        return {}, None

    try:
        with console.progress(
            description="Inspecting GitHub",
            total=len(prepared_revisions_by_bookmark),
        ) as progress:
            return (
                lookup_pull_request_lookups(
                    github_repository=github_target.repository,
                    on_progress=progress.advance,
                    prepared_revisions=tuple(prepared_revisions_by_bookmark.values()),
                ),
                None,
            )
    except CliError as error:
        return {}, error_message(error)


def _tracked_prepared_revisions_by_bookmark(
    *,
    prepared_discovered: tuple[_PreparedDiscoveredStack, ...],
) -> dict[str, PreparedRevision]:
    prepared_revisions_by_bookmark: dict[str, PreparedRevision] = {}
    for item in prepared_discovered:
        for prepared_revision in item.prepared.status_revisions:
            cached_change = prepared_revision.cached_change
            if not classify_saved_review_change(
                cached_change,
                local="present",
            ).saved_review_identity:
                continue
            prepared_revisions_by_bookmark[prepared_revision.bookmark] = prepared_revision
    return prepared_revisions_by_bookmark


def _format_pull_request_range(numbers: tuple[int, ...]) -> str:
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"PR {numbers[0]}"
    if numbers == tuple(range(numbers[0], numbers[-1] + 1)):
        return f"PRs {numbers[0]}-{numbers[-1]}"
    return "PRs " + ", ".join(f"{number}" for number in numbers)


def _tracked_pinned_bookmarks_for_repo_inspection(
    *,
    revisions: tuple[LocalRevision, ...],
    state: ReviewState,
) -> tuple[str, ...] | None:
    tracked_revisions = tuple(
        revision
        for revision in revisions
        if (cached := state.changes.get(revision.change_id)) is not None
        and _saved_change_requires_bookmark_inspection(cached)
    )
    return pinned_bookmarks_for_revisions(
        revisions=tracked_revisions,
        state=state,
    )


def _saved_change_requires_bookmark_inspection(cached_change: CachedChange) -> bool:
    review_status = classify_saved_review_change(cached_change, local="present")
    return review_status.saved_review_identity or review_status.link == "unlinked"


def _ensure_unique_repo_bookmarks(
    prepared_discovered: tuple[_PreparedDiscoveredStack, ...],
) -> None:
    bookmarks_to_changes: dict[str, list[str]] = {}
    for item in prepared_discovered:
        for prepared_revision in item.prepared.status_revisions:
            bookmarks_to_changes.setdefault(
                prepared_revision.bookmark,
                [],
            ).append(prepared_revision.revision.change_id)

    duplicates = {
        bookmark: sorted(set(change_ids))
        for bookmark, change_ids in bookmarks_to_changes.items()
        if len(set(change_ids)) > 1
    }
    if not duplicates:
        return

    collisions = ui.join(
        lambda item: t"{ui.bookmark(item[0])} for changes {ui.join(ui.change_id, item[1])}",
        sorted(duplicates.items()),
    )
    raise CliError(
        t"Could not safely inspect stacks: multiple changes resolve to the same "
        t"bookmark: {collisions}.",
        hint="Repair the saved bookmark linkage before retrying.",
    )


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
            row.review,
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
                orphan.review,
                orphan.state,
                orphan.subject,
            )
        )
    return ui.DataTable(
        columns=(
            ui.TableColumn("head", no_wrap=True),
            ui.TableColumn("size", no_wrap=True),
            ui.TableColumn("review", no_wrap=True),
            ui.TableColumn("state"),
            ui.TableColumn("description"),
        ),
        pad_edge=False,
        padding=(0, 0),
        show_edge=False,
        rows=tuple(stack_table_rows),
    )
