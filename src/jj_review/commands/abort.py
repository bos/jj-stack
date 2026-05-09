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
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.errors import error_message
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import ParsedGithubRepo, parse_github_repo
from jj_review.jj import JjCliArgs, JjClient, JjCommandError
from jj_review.models.intent import (
    AbortIntent,
    CleanupRebaseIntent,
    CloseIntent,
    LandIntent,
    LoadedIntent,
    RelinkIntent,
    SubmitIntent,
)
from jj_review.models.review_state import CachedChange
from jj_review.review.intents import (
    describe_intent,
    intent_is_stale,
)
from jj_review.review.submit_recovery import recorded_submit_still_exists_exactly
from jj_review.state.intents import write_new_intent
from jj_review.state.store import ReviewStateStore
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
    """Outcome of aborting one intent."""

    actions: tuple[AbortAction, ...]
    applied: bool
    dry_run: bool
    intent_kind: str
    intent_label: Message
    intent_started_at: str


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
    return _run_abort(
        context=context,
        options=AbortOptions(dry_run=dry_run),
    )


def _run_abort(
    *,
    context: CommandContext,
    options: AbortOptions,
) -> int:
    state_store = context.state_store
    jj_client = context.jj_client
    loaded_intents = state_store.list_intents()

    # Separate any AbortIntent lock files from the real operation intents.
    # A live-PID AbortIntent means another abort is already running; bail.
    # A dead-PID AbortIntent is a stale lock from a previous crash; clean it up.
    abort_locks = [loaded for loaded in loaded_intents if isinstance(loaded.intent, AbortIntent)]
    operation_intents = [
        loaded for loaded in loaded_intents if not isinstance(loaded.intent, AbortIntent)
    ]

    for loaded in abort_locks:
        if pid_is_alive(loaded.intent.pid):
            console.output(
                f"Another abort operation is already in progress "
                f"(PID {loaded.intent.pid}). "
                "Wait for it to finish, then run abort again."
            )
            return 1
        loaded.path.unlink(missing_ok=True)

    loaded_intents = operation_intents

    if not loaded_intents:
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
        for loaded in loaded_intents
        if not intent_is_stale(loaded.intent, _resolve_change_id)
    ]

    if not outstanding:
        count = len(loaded_intents)
        noun = "operation" if count == 1 else "operations"
        console.output(
            t"{count} stale incomplete {noun} found (changes no longer exist in this repo). "
            t"Run {ui.cmd('cleanup')} to remove stale jj-review data."
        )
        return 1

    # Refuse to retract intents whose process is still running — aborting a
    # live operation would race against it and corrupt shared state.
    live = [loaded for loaded in outstanding if pid_is_alive(loaded.intent.pid)]
    outstanding = [loaded for loaded in outstanding if not pid_is_alive(loaded.intent.pid)]

    for loaded in live:
        console.output(
            f"{loaded.intent.label} is still in progress "
            f"(PID {loaded.intent.pid}) — wait for it to finish, then run abort again."
        )

    if not outstanding:
        return 1

    # Write an abort lock so concurrent abort processes bail rather than
    # racing. The lock is removed in the finally block regardless of outcome.
    abort_lock_path = write_new_intent(
        state_store.state_dir,
        AbortIntent(
            kind="abort",
            pid=os.getpid(),
            label="abort",
            started_at=datetime.now(UTC).isoformat(),
        ),
    )

    exit_code = 1 if live else 0
    try:
        for loaded in outstanding:
            result = asyncio.run(
                _abort_intent_async(
                    context=context,
                    loaded=loaded,
                    options=options,
                )
            )
            _print_abort_result(result)
            if not result.applied and not result.dry_run:
                exit_code = 1
    finally:
        abort_lock_path.unlink(missing_ok=True)

    return exit_code


# ---------------------------------------------------------------------------
# Per-intent dispatch
# ---------------------------------------------------------------------------


async def _abort_intent_async(
    *,
    context: CommandContext,
    loaded: LoadedIntent,
    options: AbortOptions,
) -> AbortResult:
    intent = loaded.intent
    dry_run = options.dry_run

    if isinstance(intent, SubmitIntent):
        return await _abort_submit(
            dry_run=dry_run,
            intent=intent,
            intent_path=loaded.path,
            jj_client=context.jj_client,
            state_store=context.state_store,
        )

    # For all other intent types, only the intent file can be removed.  The
    # operations themselves either mutate local jj history in ways that aren't
    # straightforwardly reversible (cleanup rebase, land) or have no per-change state
    # tracked in the intent (cleanup, relink, close).
    note = _non_submit_note(intent)
    actions: list[AbortAction] = []
    if note:
        actions.append(AbortAction(kind="note", body=note, status="skipped"))
    _plan_intent_file_removal(actions=actions, dry_run=dry_run)
    if not dry_run:
        loaded.path.unlink(missing_ok=True)

    return AbortResult(
        actions=tuple(actions),
        applied=not dry_run,
        dry_run=dry_run,
        intent_kind=intent.kind,
        intent_label=describe_intent(intent),
        intent_started_at=intent.started_at,
    )


def _non_submit_note(intent) -> Message | None:
    if isinstance(intent, LandIntent):
        return t"Landing cannot be retracted; changes already merged to trunk are " \
            t"permanent. The interrupted-operation notice will be cleared so future " \
            t"commands can proceed. Run {ui.cmd('status')} to inspect the current state."
    if isinstance(intent, CleanupRebaseIntent):
        return t"Rebase changes to local jj history cannot be automatically reversed. " \
            t"The interrupted-operation notice will be cleared. Inspect with " \
            t"{ui.cmd('jj log')} and repair manually if needed."
    if isinstance(intent, CloseIntent):
        return t"Close operations cannot be automatically reversed here. " \
            t"The interrupted-operation notice will be cleared. Run {ui.cmd('status')} " \
            t"to inspect which pull requests were closed, and reopen them on GitHub " \
            t"if needed."
    if isinstance(intent, RelinkIntent):
        return t"Relink changes which PR a change tracks in local data. " \
            t"The interrupted-operation notice will be cleared. Run {ui.cmd('status')} " \
            t"to confirm the current link state looks correct."
    return None


# ---------------------------------------------------------------------------
# Interrupted submit abort
# ---------------------------------------------------------------------------


async def _abort_submit(
    *,
    dry_run: bool,
    intent: SubmitIntent,
    intent_path: Path,
    jj_client: JjClient,
    state_store: ReviewStateStore,
) -> AbortResult:
    """Retract a partial submit: close PRs, delete remote branches, clear state."""

    actions: list[AbortAction] = []
    if not _submit_intent_matches_recorded_stack(intent=intent, jj_client=jj_client):
        if not _submit_intent_head_visible(intent=intent, jj_client=jj_client):
            selector = _submit_intent_selector(intent)
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
            _plan_intent_file_removal(
                actions=actions,
                dry_run=dry_run,
            )
            if not dry_run:
                intent_path.unlink(missing_ok=True)
            return AbortResult(
                actions=tuple(actions),
                applied=not dry_run,
                dry_run=dry_run,
                intent_kind=intent.kind,
                intent_label=describe_intent(intent),
                intent_started_at=intent.started_at,
            )

        actions.append(
            AbortAction(
                kind="submit intent",
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
                t"{ui.cmd(f'jj-review submit {_submit_intent_selector(intent)}')}; "
                t"to retract the partial work, run "
                t"{ui.cmd(f'jj-review close --cleanup {_submit_intent_selector(intent)}')}",
                status="skipped",
            )
        )
        return AbortResult(
            actions=tuple(actions),
            applied=False,
            dry_run=dry_run,
            intent_kind=intent.kind,
            intent_label=describe_intent(intent),
            intent_started_at=intent.started_at,
        )

    state = state_store.load()
    next_changes = dict(state.changes)
    remote_name = intent.remote_name
    recorded_github_repository = ParsedGithubRepo(
        host=intent.github_host,
        owner=intent.github_owner,
        repo=intent.github_repo,
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
            total=len(intent.ordered_change_ids),
        ) as progress:
            for change_id in intent.ordered_change_ids:
                ok = await _retract_one_change(
                    actions=actions,
                    bookmark=intent.bookmarks.get(change_id),
                    cached=state.changes.get(change_id),
                    change_id=change_id,
                    dry_run=dry_run,
                    github_client=github_client,
                    github_repository=recorded_github_repository,
                    jj_client=jj_client,
                    next_changes=next_changes,
                    remote_branch_cleanup_block=remote_branch_cleanup_block,
                    remote_name=remote_name,
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
        _plan_intent_file_removal(actions=actions, dry_run=dry_run)
        if not dry_run:
            intent_path.unlink(missing_ok=True)
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
        intent_kind=intent.kind,
        intent_label=describe_intent(intent),
        intent_started_at=intent.started_at,
    )


def _submit_intent_matches_recorded_stack(
    *,
    intent: SubmitIntent,
    jj_client: JjClient,
) -> bool:
    """Return True when the recorded submit stack still exists exactly."""

    revisions_by_change_id = jj_client.query_revisions_by_change_ids(intent.ordered_change_ids)
    commit_ids_by_change_id: dict[str, str] = {}
    for change_id in intent.ordered_change_ids:
        revisions = revisions_by_change_id.get(change_id, ())
        if len(revisions) != 1:
            return False
        commit_ids_by_change_id[change_id] = revisions[0].commit_id

    return recorded_submit_still_exists_exactly(
        intent=intent,
        commit_ids_by_change_id=commit_ids_by_change_id,
    )


def _submit_intent_head_visible(
    *,
    intent: SubmitIntent,
    jj_client: JjClient,
) -> bool:
    """Return True when the interrupted submit's top change still resolves."""

    if not intent.ordered_change_ids:
        return False
    head_change_id = intent.ordered_change_ids[-1]
    return bool(
        jj_client.query_revisions_by_change_ids((head_change_id,)).get(head_change_id, ())
    )


def _submit_intent_selector(intent: SubmitIntent) -> str:
    if intent.ordered_change_ids:
        return short_change_id(intent.ordered_change_ids[-1])
    return intent.display_revset


async def _retract_one_change(
    *,
    actions: list[AbortAction],
    bookmark: str | None,
    cached: CachedChange | None,
    change_id: str,
    dry_run: bool,
    github_client: GithubClient,
    github_repository,
    jj_client: JjClient,
    next_changes: dict[str, CachedChange],
    remote_branch_cleanup_block: Message | None,
    remote_name: str | None,
) -> bool:
    """Retract one change. Returns True if all steps succeeded (nothing blocked)."""

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
        dry_run=dry_run,
        jj_client=jj_client,
        next_changes=next_changes,
        remote_branch_cleanup_block=remote_branch_cleanup_block,
        remote_name=remote_name,
    )
    return pr_ok and local_ok


def _retract_one_change_local(
    *,
    actions: list[AbortAction],
    bookmark: str | None,
    cached: CachedChange | None,
    change_id: str,
    dry_run: bool,
    jj_client: JjClient,
    next_changes: dict[str, CachedChange],
    remote_branch_cleanup_block: Message | None,
    remote_name: str | None,
) -> bool:
    """Retract local state for one change. Returns True if no steps were blocked."""

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


def _plan_intent_file_removal(
    *,
    actions: list[AbortAction],
    dry_run: bool,
) -> None:
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
        header = t"Planned abort actions for {result.intent_label}:"
    elif result.applied:
        header = t"Applied abort actions for {result.intent_label}:"
    else:
        header = t"Abort incomplete for {result.intent_label}:"

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
