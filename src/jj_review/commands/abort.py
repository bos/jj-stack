"""Undo interrupted jj-review operations.

Finds all interrupted jj-review operations in this repo. For each one, abort
retracts completed work when it can do so safely (closes opened PRs, deletes
pushed review branches, forgets local bookmarks, clears tracking data) and
cleans up the leftover interrupted-operation state.

Use `--dry-run` to preview what would be undone without changing anything.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.commands._operation_lock import mutating_command_lock
from jj_review.errors import error_message
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import ParsedGithubRepo, parse_github_repo
from jj_review.jj import JjCliArgs, JjClient, JjCommandError
from jj_review.models.review_state import CachedChange
from jj_review.review.operations import describe_operation, operation_kind
from jj_review.review.submit_recovery import recorded_submit_still_exists_exactly
from jj_review.state.journal import (
    CleanupOperationRecord,
    CleanupRebaseOperationRecord,
    CloseOperationRecord,
    LandOperationRecord,
    LoadedOperationRecord,
    RelinkOperationRecord,
    SubmitOperationRecord,
    append_abandoned_event,
)
from jj_review.system import pid_is_alive
from jj_review.ui import Message, plain_text

HELP = "Undo interrupted jj-review operations"

logger = logging.getLogger(__name__)

AbortActionStatus = Literal["applied", "blocked", "planned", "skipped"]
type AbortActionBody = Message


@dataclass(frozen=True, slots=True)
class AbortOptions:
    """Parsed command options for `abort`."""

    dry_run: bool


@dataclass(frozen=True, slots=True)
class AbortAction:
    """One retraction step that was planned, applied, blocked, or skipped."""

    kind: str
    body: AbortActionBody
    status: AbortActionStatus

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class AbortResult:
    """Outcome of aborting one operation."""

    actions: tuple[AbortAction, ...]
    applied: bool
    dry_run: bool
    operation_kind: str
    operation_label: Message
    operation_started_at: str


@dataclass(frozen=True, slots=True)
class AbortRun:
    """Shared abort execution context for one command invocation."""

    context: CommandContext
    options: AbortOptions

    @property
    def dry_run(self) -> bool:
        return self.options.dry_run


def abort(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `abort`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with mutating_command_lock(command="abort", context=context):
        return _run_abort(
            context=context,
            options=_abort_options_from_cli(dry_run=dry_run),
        )


def _abort_options_from_cli(*, dry_run: bool) -> AbortOptions:
    return AbortOptions(dry_run=dry_run)


def _run_abort(
    *,
    context: CommandContext,
    options: AbortOptions,
) -> int:
    state_store = context.state_store
    jj_client = context.jj_client
    loaded_operations = state_store.list_operations()

    if not loaded_operations:
        console.output("Nothing to abort.")
        return 0

    def _resolve_change_id(change_id: str) -> bool:
        try:
            jj_client.resolve_revision(change_id)
            return True
        except (JjCommandError, Exception):
            return False

    outstanding = [
        loaded
        for loaded in loaded_operations
        if not _operation_notice_is_stale(loaded, _resolve_change_id)
    ]

    if not outstanding:
        count = len(loaded_operations)
        noun = "operation" if count == 1 else "operations"
        console.output(
            t"{count} stale incomplete {noun} found (changes no longer exist in this repo). "
            t"Run {ui.cmd('cleanup')} to remove stale jj-review data."
        )
        return 1

    # Refuse to retract operations whose process is still running — aborting a
    # live operation would race against it and corrupt shared state.
    live = [loaded for loaded in outstanding if pid_is_alive(loaded.operation.pid)]
    outstanding = [loaded for loaded in outstanding if not pid_is_alive(loaded.operation.pid)]

    for loaded in live:
        console.output(
            t"{describe_operation(loaded.operation)} is still in progress "
            t"(PID {loaded.operation.pid}) — wait for it to finish, then run abort again."
        )

    if not outstanding:
        return 1

    run = AbortRun(context=context, options=options)
    exit_code = 1 if live else 0
    for loaded in outstanding:
        result = asyncio.run(
            _abort_operation_async(
                loaded=loaded,
                run=run,
            )
        )
        _print_abort_result(result)
        if not result.applied and not result.dry_run:
            exit_code = 1

    return exit_code


# ---------------------------------------------------------------------------
# Per-operation dispatch
# ---------------------------------------------------------------------------


async def _abort_operation_async(
    *,
    loaded: LoadedOperationRecord,
    run: AbortRun,
) -> AbortResult:
    operation = loaded.operation
    dry_run = run.dry_run

    if isinstance(operation, SubmitOperationRecord):
        return await _abort_submit(
            operation=operation,
            loaded=loaded,
            run=run,
        )

    # For all other operation types, only the operation record can be cleared. The
    # operations themselves either mutate local jj history in ways that aren't
    # straightforwardly reversible (cleanup rebase, land) or have no reversible
    # per-change state tracked in the record (cleanup, relink, close).
    note = _non_submit_note(operation)
    actions: list[AbortAction] = []
    if note:
        actions.append(AbortAction(kind="note", body=note, status="skipped"))
    _plan_operation_notice_removal(actions=actions, run=run)
    if not dry_run:
        _clear_operation_notice(loaded, reason="abort")

    return AbortResult(
        actions=tuple(actions),
        applied=not dry_run,
        dry_run=dry_run,
        operation_kind=operation_kind(operation),
        operation_label=describe_operation(operation),
        operation_started_at=operation.started_at,
    )


def _non_submit_note(operation) -> Message | None:
    if isinstance(operation, LandOperationRecord):
        return t"Landing cannot be retracted; changes already merged to trunk are " \
            t"permanent. The interrupted-operation notice will be cleared so future " \
            t"commands can proceed. Run {ui.cmd('status')} to inspect the current state."
    if isinstance(operation, CleanupRebaseOperationRecord):
        return t"Rebase changes to local jj history cannot be automatically reversed. " \
            t"The interrupted-operation notice will be cleared. Inspect with " \
            t"{ui.cmd('jj log')} and repair manually if needed."
    if isinstance(operation, CloseOperationRecord):
        return t"Close operations cannot be automatically reversed here. " \
            t"The interrupted-operation notice will be cleared. Run {ui.cmd('status')} " \
            t"to inspect which pull requests were closed, and reopen them on GitHub " \
            t"if needed."
    if isinstance(operation, RelinkOperationRecord):
        return t"Relink changes which PR a change tracks in local data. " \
            t"The interrupted-operation notice will be cleared. Run {ui.cmd('status')} " \
            t"to confirm the current link state looks correct."
    return None


# ---------------------------------------------------------------------------
# Interrupted submit abort
# ---------------------------------------------------------------------------


async def _abort_submit(
    *,
    operation: SubmitOperationRecord,
    loaded: LoadedOperationRecord,
    run: AbortRun,
) -> AbortResult:
    """Retract a partial submit: close PRs, delete remote branches, clear state."""

    dry_run = run.dry_run
    jj_client: JjClient = run.context.jj_client
    state_store = run.context.state_store
    actions: list[AbortAction] = []
    if not _submit_operation_matches_recorded_stack(operation=operation, run=run):
        if not _submit_operation_head_visible(operation=operation, run=run):
            selector = _submit_operation_selector(operation)
            changed = "would be changed" if dry_run else "were changed"
            actions.append(
                AbortAction(
                    kind="github",
                    body=(
                        t"no pull requests or review branches {changed}; "
                        t"change {ui.change_id(selector)} is no longer visible in jj"
                    ),
                    status="skipped",
                )
            )
            _plan_operation_notice_removal(actions=actions, run=run)
            if not dry_run:
                _clear_operation_notice(loaded, reason="abort")
            return AbortResult(
                actions=tuple(actions),
                applied=not dry_run,
                dry_run=dry_run,
                operation_kind=operation_kind(operation),
                operation_label=describe_operation(operation),
                operation_started_at=operation.started_at,
            )

        actions.append(
            AbortAction(
                kind="submit operation",
                body=(
                    "current stack has changed since this submit was interrupted; "
                    "abort will not guess which pull requests or review branches to retract"
                ),
                status="blocked",
            )
        )
        actions.append(
            AbortAction(
                kind="notice",
                body=t"kept — to continue, run "
                t"{ui.cmd(f'jj-review submit {_submit_operation_selector(operation)}')}; "
                t"to retract the partial work, run "
                t"{ui.cmd(f'jj-review close --cleanup {_submit_operation_selector(operation)}')}",
                status="skipped",
            )
        )
        return AbortResult(
            actions=tuple(actions),
            applied=False,
            dry_run=dry_run,
            operation_kind=operation_kind(operation),
            operation_label=describe_operation(operation),
            operation_started_at=operation.started_at,
        )

    state = state_store.load()
    next_changes = dict(state.changes)
    remote_name = operation.remote_name
    recorded_github_repository = ParsedGithubRepo(
        host=operation.github_host,
        owner=operation.github_owner,
        repo=operation.github_repo,
    )
    remotes_by_name = {remote.name: remote for remote in jj_client.list_git_remotes()}
    remote_branch_cleanup_block: Message | None = None
    if (recorded_remote := remotes_by_name.get(remote_name)) is None:
        remote_branch_cleanup_block = t"recorded remote {ui.bookmark(remote_name)} is no " \
            t"longer configured; abort will not guess where to delete review branches"
    else:
        current_github_repository = parse_github_repo(recorded_remote)
        if current_github_repository != recorded_github_repository:
            remote_branch_cleanup_block = (
                t"recorded remote {ui.bookmark(remote_name)} no longer points at "
                t"{recorded_github_repository.full_name}; abort will not guess where to "
                t"delete review branches"
            )

    per_change_ok: list[bool] = []

    async with build_github_client(
        base_url=recorded_github_repository.api_base_url
    ) as github_client:
        with console.progress(
            description="Retracting submitted changes",
            total=len(operation.ordered_change_ids),
        ) as progress:
            for change_id in operation.ordered_change_ids:
                ok = await _retract_one_change(
                    actions=actions,
                    bookmark=operation.bookmarks.get(change_id),
                    cached=state.changes.get(change_id),
                    change_id=change_id,
                    github_client=github_client,
                    github_repository=recorded_github_repository,
                    next_changes=next_changes,
                    remote_branch_cleanup_block=remote_branch_cleanup_block,
                    remote_name=remote_name,
                    run=run,
                )
                per_change_ok.append(ok)
                progress.advance()

    all_retracted = all(per_change_ok) if per_change_ok else True

    if next_changes != dict(state.changes):
        verb = "would clear" if dry_run else "cleared"
        actions.append(
            AbortAction(
                kind="saved state",
                body=f"{verb} tracking data for aborted changes",
                status="planned" if dry_run else "applied",
            )
        )
    if not dry_run and next_changes != dict(state.changes):
        state_store.save(state.model_copy(update={"changes": next_changes}))

    if all_retracted or dry_run:
        _plan_operation_notice_removal(actions=actions, run=run)
        if not dry_run:
            _clear_operation_notice(loaded, reason="abort")
    else:
        actions.append(
            AbortAction(
                kind="notice",
                body=(
                    t"kept — fix the blocked steps above, "
                    t"then run {ui.cmd('abort')} again to retry"
                ),
                status="skipped",
            )
        )

    return AbortResult(
        actions=tuple(actions),
        applied=all_retracted and not dry_run,
        dry_run=dry_run,
        operation_kind=operation_kind(operation),
        operation_label=describe_operation(operation),
        operation_started_at=operation.started_at,
    )


def _submit_operation_matches_recorded_stack(
    *,
    operation: SubmitOperationRecord,
    run: AbortRun,
) -> bool:
    """Return True when the recorded submit stack still exists exactly."""

    jj_client = run.context.jj_client
    revisions_by_change_id = jj_client.query_revisions_by_change_ids(operation.ordered_change_ids)
    commit_ids_by_change_id: dict[str, str] = {}
    for change_id in operation.ordered_change_ids:
        revisions = revisions_by_change_id.get(change_id, ())
        if len(revisions) != 1:
            return False
        commit_ids_by_change_id[change_id] = revisions[0].commit_id

    return recorded_submit_still_exists_exactly(
        operation=operation,
        commit_ids_by_change_id=commit_ids_by_change_id,
    )


def _submit_operation_head_visible(
    *,
    operation: SubmitOperationRecord,
    run: AbortRun,
) -> bool:
    """Return True when the interrupted submit's top change still resolves."""

    if not operation.ordered_change_ids:
        return False
    head_change_id = operation.ordered_change_ids[-1]
    jj_client = run.context.jj_client
    return bool(
        jj_client.query_revisions_by_change_ids((head_change_id,)).get(head_change_id, ())
    )


def _submit_operation_selector(operation: SubmitOperationRecord) -> str:
    if operation.ordered_change_ids:
        return short_change_id(operation.ordered_change_ids[-1])
    return operation.display_revset


async def _retract_one_change(
    *,
    actions: list[AbortAction],
    bookmark: str | None,
    cached: CachedChange | None,
    change_id: str,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    remote_branch_cleanup_block: Message | None,
    remote_name: str | None,
    run: AbortRun,
) -> bool:
    """Retract one change. Returns True if all steps succeeded (nothing blocked)."""

    dry_run = run.dry_run
    pr_ok = True

    pr_number = cached.pr_number if cached is not None else None
    pr_state = cached.pr_state if cached is not None else None

    if pr_number is not None and pr_state not in ("closed", "merged"):
        action_body = t"close PR #{pr_number} for {ui.change_id(change_id)}"
        if dry_run:
            actions.append(AbortAction(kind="pull request", body=action_body, status="planned"))
        else:
            try:
                await github_client.close_pull_request(
                    github_repository.owner,
                    github_repository.repo,
                    pull_number=pr_number,
                )
                actions.append(
                    AbortAction(kind="pull request", body=action_body, status="applied")
                )
            except GithubClientError as error:
                # 404: PR no longer exists. 422: PR is already closed.
                # Either way the desired end state (closed/gone) is already reached.
                if error.status_code in (404, 422):
                    actions.append(
                        AbortAction(kind="pull request", body=action_body, status="applied")
                    )
                else:
                    actions.append(
                        AbortAction(
                            kind="pull request",
                            body=(
                                t"could not close PR #{pr_number} for "
                                t"{ui.change_id(change_id)}: {error_message(error)}"
                            ),
                            status="blocked",
                        )
                    )
                    pr_ok = False

    local_ok = _retract_one_change_local(
        actions=actions,
        bookmark=bookmark,
        cached=cached,
        change_id=change_id,
        next_changes=next_changes,
        remote_branch_cleanup_block=remote_branch_cleanup_block,
        remote_name=remote_name,
        run=run,
    )
    return pr_ok and local_ok


def _retract_one_change_local(
    *,
    actions: list[AbortAction],
    bookmark: str | None,
    cached: CachedChange | None,
    change_id: str,
    next_changes: dict[str, CachedChange],
    remote_branch_cleanup_block: Message | None,
    remote_name: str | None,
    run: AbortRun,
) -> bool:
    """Retract local state for one change. Returns True if no steps were blocked."""

    dry_run = run.dry_run
    jj_client = run.context.jj_client
    any_blocked = False

    if bookmark is not None:
        bm_state = jj_client.get_bookmark_state(bookmark)

        if remote_name is not None:
            branch_label = f"{bookmark}@{remote_name}"
            if remote_branch_cleanup_block is not None:
                actions.append(
                    AbortAction(
                        kind="remote branch",
                        body=(
                            t"{remote_branch_cleanup_block} "
                            t"({ui.bookmark(branch_label)} for {ui.change_id(change_id)})"
                        ),
                        status="blocked",
                    )
                )
                any_blocked = True
            else:
                remote_target = bm_state.remote_target(remote_name)
                if remote_target is not None and len(remote_target.targets) > 1:
                    actions.append(
                        AbortAction(
                            kind="remote branch",
                            body=(
                                t"{ui.bookmark(branch_label)} for "
                                t"{ui.change_id(change_id)} is conflicted on the remote; "
                                t"abort will not guess which target to delete"
                            ),
                            status="blocked",
                        )
                    )
                    any_blocked = True
                elif remote_target is not None and remote_target.target is not None:
                    remote_commit_id = remote_target.target
                    action_body = (
                        t"delete {ui.bookmark(branch_label)} for {ui.change_id(change_id)}"
                    )
                    if dry_run:
                        actions.append(
                            AbortAction(kind="remote branch", body=action_body, status="planned")
                        )
                    else:
                        try:
                            jj_client.delete_remote_bookmarks(
                                remote=remote_name,
                                deletions=((bookmark, remote_commit_id),),
                            )
                            actions.append(
                                AbortAction(
                                    kind="remote branch", body=action_body, status="applied"
                                )
                            )
                        except JjCommandError as error:
                            actions.append(
                                AbortAction(
                                    kind="remote branch",
                                    body=t"could not delete {ui.bookmark(branch_label)}: {error}",
                                    status="blocked",
                                )
                            )
                            any_blocked = True

        if bm_state.local_target is not None and not any_blocked:
            action_body = t"forget {ui.bookmark(bookmark)} for {ui.change_id(change_id)}"
            if dry_run:
                actions.append(
                    AbortAction(kind="local bookmark", body=action_body, status="planned")
                )
            else:
                try:
                    jj_client.forget_bookmarks((bookmark,))
                    actions.append(
                        AbortAction(kind="local bookmark", body=action_body, status="applied")
                    )
                except JjCommandError as error:
                    actions.append(
                        AbortAction(
                            kind="local bookmark",
                            body=t"could not forget {ui.bookmark(bookmark)}: {error}",
                            status="blocked",
                        )
                    )
                    any_blocked = True

    # Only remove from state cache if all local steps succeeded; if anything
    # was blocked the caller needs the cached data (PR number, bookmark) to
    # diagnose and retry.
    if not any_blocked and change_id in next_changes:
        del next_changes[change_id]

    return not any_blocked


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _operation_notice_is_stale(
    loaded: LoadedOperationRecord,
    resolve_change_id: Callable[[str], bool],
) -> bool:
    if isinstance(loaded.operation, RelinkOperationRecord | CleanupOperationRecord):
        if pid_is_alive(loaded.operation.pid):
            return False
        try:
            started = datetime.fromisoformat(loaded.operation.started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            return True
        return (datetime.now(UTC) - started).days >= 7
    ids = loaded.operation.change_ids()
    if not ids:
        return False
    return not any(resolve_change_id(change_id) for change_id in ids)


def _clear_operation_notice(
    loaded: LoadedOperationRecord,
    *,
    reason: str,
) -> None:
    append_abandoned_event(loaded.path, reason=reason)


def _plan_operation_notice_removal(
    *,
    actions: list[AbortAction],
    run: AbortRun,
) -> None:
    dry_run = run.dry_run
    body = (
        "would clear it from future status output"
        if dry_run
        else "cleared it from future status output"
    )
    actions.append(
        AbortAction(
            kind="notice",
            body=body,
            status="planned" if dry_run else "applied",
        )
    )


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def _print_abort_result(result: AbortResult) -> None:
    if result.dry_run:
        header = t"Planned abort actions for {result.operation_label}:"
    elif result.applied:
        header = t"Applied abort actions for {result.operation_label}:"
    else:
        header = t"Abort incomplete for {result.operation_label}:"

    console.output(header)
    for action in result.actions:
        prefix, prefix_style, body_style = _abort_action_presentation(action.status)
        console.output(
            ui.prefixed_line(
                f"{prefix} ",
                (ui.semantic_text(action.kind, "prefix"), ": ", action.body),
                prefix_labels=prefix_style,
                message_labels=body_style,
            )
        )


def _abort_action_presentation(
    status: AbortActionStatus,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    if status == "applied":
        return (
            "  ✓",
            ("signature status good",),
            None,
        )
    if status == "planned":
        return (
            "  ~",
            ("hint heading",),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ("error heading",),
            ("warning heading",),
        )
    if status == "skipped":
        return (
            "  -",
            ("hidden prefix", "rest"),
            None,
        )
    return ("  ?", None, None)
