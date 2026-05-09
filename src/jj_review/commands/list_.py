"""List review stacks in this repository.

Shows one row per review stack in this repo, including the head change ID, stack size, review
state, and description of the head commit.

It also shows orphaned PRs: open PRs that `jj-review` still knows about, but whose local change
is no longer part of any current review stack. Close those explicitly with
`jj-review close --cleanup --pull-request <pr>`.

`--fetch` runs a fetch first so the report uses current remote branch locations.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.console import requested_color_mode
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.github.resolution import ParsedGithubRepo, parse_github_repo, select_submit_remote
from jj_review.jj import JjCliArgs, JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_review.review.discovery import discover_tracked_stacks
from jj_review.review.status import (
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
from jj_review.review.topology import enumerate_orphaned_records, submitted_state_disagreement
from jj_review.state.store import ReviewStateStore

HELP = "List review stacks in this repo"


@dataclass(frozen=True, slots=True)
class StackRow:
    current: bool
    head_change_id: str
    incomplete: bool
    review: str
    size: int
    state: ui.Message
    subject: str


@dataclass(frozen=True, slots=True)
class OrphanRow:
    """One orphaned PR — its local change has left every current stack."""

    change_id: str
    hint: str | None
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
    github_error: ErrorMessage | None
    github_repository: ParsedGithubRepo | None
    remote: GitRemote | None
    remote_error: ErrorMessage | None


def list_(
    *,
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
    if fetch:
        refresh_remote_state_for_status(jj_client=context.jj_client)

    state_store = ReviewStateStore.for_repo(context.repo_root)
    state = state_store.load()
    with console.spinner(description="Inspecting local stacks"):
        discovered = discover_tracked_stacks(jj_client=context.jj_client, state=state)

    ordered = _order_discovered_stacks(
        discovered.stacks,
        current_commit_id=discovered.current_commit_id,
        jj_client=context.jj_client,
    )
    orphan_rows = tuple(
        _build_orphan_row(orphan)
        for orphan in enumerate_orphaned_records(state, ordered)
    )
    if not ordered:
        if not orphan_rows:
            console.output("No review stacks.")
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
            config=context.config,
            discovered=ordered,
            jj_client=context.jj_client,
            state=state,
        )
    prepared_discovered = tuple(
        _PreparedDiscoveredStack(
            current=_stack_contains_commit_id(
                stack,
                commit_id=discovered.current_commit_id,
            ),
            prepared=prepare_stack_for_status(
                bookmark_states=repo_inspection.bookmark_states,
                config=context.config,
                jj_client=context.jj_client,
                persist_bookmarks=False,
                remote=repo_inspection.remote,
                remote_error=repo_inspection.remote_error,
                stack=stack,
                state=state,
                state_store=state_store,
            ),
        )
        for stack in ordered
    )
    _ensure_unique_repo_bookmarks(prepared_discovered)
    pull_request_lookups, github_error = _load_pull_request_lookups(
        github_repository=repo_inspection.github_repository,
        prepared_discovered=prepared_discovered,
    )
    rows = tuple(
        _build_row(
            github_error=repo_inspection.github_error or github_error,
            is_current=item.current,
            prepared_stack=item.prepared,
            pull_request_lookups=pull_request_lookups,
        )
        for item in prepared_discovered
    )
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


def _build_orphan_row(orphan) -> OrphanRow:
    pr_number = orphan.cached_change.pr_number
    return OrphanRow(
        change_id=orphan.change_id,
        hint=(
            f"close --cleanup --pull-request {pr_number}"
            if pr_number is not None
            else None
        ),
        review=f"PR #{pr_number}" if pr_number is not None else "(no PR number)",
        state=ui.semantic_text("orphan", "warning", "heading"),
        subject="local change missing",
    )


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
                t"inspect with {ui.cmd(f'jj-review status {head}')} or refresh with "
                t"{ui.cmd(f'jj-review submit {head}')}.",
            )
        )
        return
    heads_fragments = ui.join(ui.change_id, stale_heads)
    console.warning(
        (
            "Tracked stacks have changed since their last submit; ",
            t"inspect with {ui.cmd('jj-review status <head>')} or refresh with "
            t"{ui.cmd('jj-review submit <head>')}: ",
            *heads_fragments,
        )
    )


def _prepare_repo_inspection_context(
    *,
    config,
    discovered: tuple[LocalStack, ...],
    jj_client: JjClient,
    state: ReviewState,
) -> _RepoInspectionContext:
    remotes = jj_client.list_git_remotes()
    remote: GitRemote | None = None
    remote_error: ErrorMessage | None = None
    if remotes:
        try:
            remote = select_submit_remote(remotes)
        except CliError as error:
            remote_error = error_message(error)

    all_revisions = tuple(revision for stack in discovered for revision in stack.revisions)
    bookmark_states: dict[str, BookmarkState] = {}
    if remote is not None or config.use_bookmarks:
        pinned_bookmarks = _tracked_pinned_bookmarks_for_repo_inspection(
            revisions=all_revisions,
            state=state,
        )
        bookmark_states = jj_client.list_bookmark_states(pinned_bookmarks)

    github_repository = parse_github_repo(remote) if remote is not None else None
    github_error: ErrorMessage | None = None
    if remote is not None and github_repository is None:
        github_error = f"Could not determine the GitHub repository for remote {remote.name}."

    return _RepoInspectionContext(
        bookmark_states=bookmark_states,
        github_error=github_error,
        github_repository=github_repository,
        remote=remote,
        remote_error=remote_error,
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
    review = _pull_request_range_from_revisions(revisions)
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
    )
    return StackRow(
        current=is_current,
        head_change_id=stack.head.change_id,
        incomplete=_status_is_incomplete(
            github_error=github_error,
            remote_error=prepared_stack.remote_error,
            revisions=revisions,
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
) -> ui.Message:
    fragments = [
        *local_fragments,
        *_status_fragments(
            github_error=github_error,
            remote_error=remote_error,
            revisions=revisions,
        ),
    ]
    if fragments:
        joined: list[ui.Message] = []
        for index, fragment in enumerate(fragments):
            if index:
                joined.append(", ")
            joined.append(fragment)
        return tuple(joined)
    if any(
        classify_review_status_revision(revision).saved_review_identity
        for revision in revisions
    ):
        return "tracked"
    return "not submitted"


def _status_fragments(
    *,
    github_error: ErrorMessage | None,
    remote_error: ErrorMessage | None,
    revisions: tuple[ReviewStatusRevision, ...],
) -> tuple[ui.Message, ...]:
    fragments: list[ui.Message] = []
    statuses = tuple(classify_review_status_revision(revision) for revision in revisions)
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

    lookup_failures = sum(
        1 for status in statuses if status.has_pull_request_lookup_failure
    )
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
        if _is_open_published(status)
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
    revisions: tuple[ReviewStatusRevision, ...],
) -> bool:
    if github_error is not None or remote_error is not None:
        return True
    return any(
        _status_is_incomplete_change(classify_review_status_revision(revision))
        for revision in revisions
    )


def _status_is_incomplete_change(status: ReviewChangeStatus) -> bool:
    return (
        status.has_stale_pull_request_link
        or status.has_pull_request_lookup_failure
        or status.pr_lifecycle == "ambiguous"
    )


def _is_open_published(status: ReviewChangeStatus) -> bool:
    return status.pr_lifecycle == "open" and status.pr_draft is False


def _pull_request_range_from_revisions(revisions: tuple[ReviewStatusRevision, ...]) -> str:
    numbers: list[int] = []
    for revision in revisions:
        lookup = revision.pull_request_lookup
        if lookup is not None and lookup.pull_request is not None:
            numbers.append(lookup.pull_request.number)
            continue
        cached_change = revision.cached_change
        if cached_change is not None and cached_change.pr_number is not None:
            numbers.append(cached_change.pr_number)
    return _format_pull_request_range(tuple(numbers))


def _load_pull_request_lookups(
    *,
    github_repository: ParsedGithubRepo | None,
    prepared_discovered: tuple[_PreparedDiscoveredStack, ...],
) -> tuple[dict[str, PullRequestLookup], ErrorMessage | None]:
    if github_repository is None:
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
                    github_repository=github_repository,
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
    unique = tuple(sorted(dict.fromkeys(numbers)))
    if not unique:
        return ""
    if len(unique) == 1:
        return f"PR {unique[0]}"
    if unique == tuple(range(unique[0], unique[-1] + 1)):
        return f"PRs {unique[0]}-{unique[-1]}"
    return "PRs " + ", ".join(f"{number}" for number in unique)


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
        t"Could not safely inspect review stacks: multiple changes resolve to the same "
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
