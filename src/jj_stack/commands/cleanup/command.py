"""Find and remove stale tracking data and review branches left behind by earlier review work.

By default, this runs a repo-wide cleanup of tracking data and review branches that no longer
match an active review. With `--rebase [REVSET]`, it works on one local stack instead.

Open orphaned PRs are preserved. Run `jj-stack list` to see them, then retire one explicitly
with `jj-stack unstack --cleanup --pull-request <pr>`.

Use `cleanup --rebase` when some changes from your stack have been merged on GitHub as rewritten
commits (e.g. via a squash merge in the GitHub UI). In this case, your local stack still
contains the old pre-merge commits, and `cleanup --rebase` will drop those merged ancestors from
the local stack and rebase the remaining local changes onto the current `trunk()`.

Use `--dry-run` to preview cleanup actions without making any changes.

"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.github.client import build_github_client
from jj_stack.github.resolution import (
    resolve_github_target,
)
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.review.bookmarks import (
    bookmark_cleanup_allowed,
    classify_local_bookmark_forget,
    is_review_bookmark,
    local_bookmark_forget_blocked_body,
)
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_change_without_pull_request,
    is_open_pr_record,
)
from jj_stack.state.journal import OperationJournal
from jj_stack.state.operation_lock import (
    acquire_operation_lock,
)

from .rebase import _run_cleanup_rebase_command
from .shared import (
    CleanupAction,
    CleanupResult,
    OrphanLocalBookmarkCleanupPlan,
    PreparedCleanup,
    PreparedCleanupChange,
    RemoteBranchCleanupPlan,
    _build_action_streamer,
    _CleanupSaver,
    _emit_output_lines,
    _emit_severity_lines,
    _render_cleanup_action_header,
    _render_cleanup_postamble,
    _render_remote_and_github_lines,
    _StaleCleanupMutationPlan,
)
from .stack_comments import (
    _run_stack_comment_cleanup_pass,
    _should_inspect_stack_comment_cleanup,
    _stack_comment_cleanup_eligibility,
)
from .stale import _plan_orphan_local_bookmark_cleanups, _stale_change_reasons

HELP = "Remove stale tracking data and review branches; optionally rebase one stack"


def cleanup(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
    rebase_revset: str | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="cleanup --rebase" if rebase_revset is not None else "cleanup",
    ):
        if rebase_revset is not None:
            return _run_cleanup_rebase_command(
                context=context,
                dry_run=dry_run,
                rebase_revset=rebase_revset,
            )

        return _run_cleanup_command(
            context=context,
            dry_run=dry_run,
        )


def _run_cleanup_command(
    *,
    context: CommandContext,
    dry_run: bool,
) -> int:
    """Render and run the stale cleanup command path."""

    with console.spinner(description="Loading bookmark state"):
        prepared_cleanup = _prepare_cleanup(
            context=context,
            dry_run=dry_run,
        )
    stale_reasons = _stale_change_reasons(
        change_ids=tuple(prepared_cleanup.state.changes),
        context=prepared_cleanup.context,
    )
    if _cleanup_needs_remote_context(
        prepared_cleanup=prepared_cleanup,
        stale_reasons=stale_reasons,
    ):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        _emit_severity_lines(
            _render_remote_and_github_lines(
                remote=prepared_cleanup.remote,
                remote_error=prepared_cleanup.remote_error,
                github_repository=prepared_cleanup.github_repository,
                github_error=prepared_cleanup.github_repository_error,
            )
        )

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                header=_render_cleanup_action_header(dry_run=prepared_cleanup.dry_run),
            ),
            prepared_cleanup=prepared_cleanup,
            stale_reasons=stale_reasons,
        )
    )
    _emit_output_lines(_render_cleanup_postamble(result=result))
    return 1 if any(action.status == "blocked" for action in result.actions) else 0


def _prepare_cleanup(
    *,
    context: CommandContext,
    dry_run: bool,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    state_store = context.state_store
    state = state_store.load()
    if not dry_run:
        state_store.require_writable()

    bookmark_states = _load_bookmark_states(
        context=context,
        state=state,
    )

    return PreparedCleanup(
        context=context,
        bookmark_states=bookmark_states,
        github_repository=None,
        github_repository_error=None,
        remote=None,
        remote_error=None,
        remote_context_loaded=False,
        dry_run=dry_run,
        state=state,
    )


async def _run_cleanup_async(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None] | None = None,
) -> CleanupResult:
    next_changes = dict(prepared_cleanup.state.changes)
    recorder = ActionRecorder[CleanupAction](on_action=on_action)
    dry_run = prepared_cleanup.dry_run

    # Write an operation journal before the first mutation on live runs only.
    journal = OperationJournal.disabled()
    _cleanup_succeeded = False
    if not dry_run:
        state_dir = prepared_cleanup.context.state_store.require_writable()
        journal = OperationJournal.begin(
            state_dir,
            operation="cleanup",
            options={},
            resolved_scope={
                "cached_change_ids": tuple(prepared_cleanup.state.changes),
            },
        )

    saver = _CleanupSaver(
        journal=journal,
        last_persisted=dict(prepared_cleanup.state.changes),
        prepared_cleanup=prepared_cleanup,
    )
    try:
        if stale_reasons is None:
            stale_reasons = _stale_change_reasons(
                change_ids=tuple(prepared_cleanup.state.changes),
                context=prepared_cleanup.context,
            )
        if _cleanup_needs_remote_context(
            prepared_cleanup=prepared_cleanup,
            stale_reasons=stale_reasons,
        ):
            prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
            saver.prepared_cleanup = prepared_cleanup
        prepared_changes = _run_local_cleanup_pass(
            journal=journal,
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=recorder.record,
            saver=saver,
            stale_reasons=stale_reasons,
        )
        if (
            prepared_cleanup.github_repository is not None
            and any(prepared_change.inspect_stack_comment for prepared_change in prepared_changes)
        ):
            github_repository = prepared_cleanup.github_repository
            async with build_github_client(repository=github_repository) as github_client:
                await _run_stack_comment_cleanup_pass(
                    github_client=github_client,
                    journal=journal,
                    next_changes=next_changes,
                    prepared_changes=prepared_changes,
                    prepared_cleanup=prepared_cleanup,
                    record_action=recorder.record,
                    saver=saver,
                )

        saver.save_if_changed(next_changes)
        _cleanup_succeeded = True
        return CleanupResult(actions=recorder.as_tuple())
    finally:
        if _cleanup_succeeded:
            journal.append(
                "completed",
                {"cached_change_ids": tuple(prepared_cleanup.state.changes)},
            )


def _run_local_cleanup_pass(
    *,
    journal: OperationJournal,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    saver: _CleanupSaver,
    stale_reasons: dict[str, str | None],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    mutation_plans: list[_StaleCleanupMutationPlan] = []
    orphan_local_bookmark_plans: list[OrphanLocalBookmarkCleanupPlan] = []
    for change_id, cached_change in prepared_cleanup.state.changes.items():
        stale_reason = stale_reasons.get(change_id)
        bookmark_state = prepared_cleanup.bookmark_states.get(
            cached_change.bookmark or "",
            BookmarkState(name=cached_change.bookmark or ""),
        )
        remote_state = (
            None
            if prepared_cleanup.remote is None
            else bookmark_state.remote_target(prepared_cleanup.remote.name)
        )
        review_status = classify_review_change_without_pull_request(
            cached_change=cached_change,
            commit_id=None,
            local="orphaned",
            remote_state=remote_state,
        )
        prepared_change = PreparedCleanupChange(
            bookmark_state=bookmark_state,
            cached_change=cached_change,
            change_id=change_id,
            inspect_stack_comment=_should_inspect_stack_comment_cleanup(
                cached_change=cached_change,
                remote=prepared_cleanup.remote,
                review_status=review_status,
                stale_reason=stale_reason,
            ),
            remote_state=remote_state,
            review_status=review_status,
            stale_reason=stale_reason,
        )
        prepared_changes.append(prepared_change)
        mutation_plan = _process_stale_cleanup_change(
            next_changes=next_changes,
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        if mutation_plan is not None:
            mutation_plans.append(mutation_plan)

    tracked_bookmarks = {
        cached_change.bookmark
        for cached_change in prepared_cleanup.state.changes.values()
        if cached_change.bookmark is not None
    }
    for orphan_plan in _plan_orphan_local_bookmark_cleanups(
        bookmark_states=prepared_cleanup.bookmark_states,
        context=prepared_cleanup.context,
        tracked_bookmarks=tracked_bookmarks,
    ):
        if prepared_cleanup.dry_run:
            record_action(orphan_plan.action)
            continue
        if orphan_plan.action.status != "planned":
            record_action(orphan_plan.action)
            continue
        orphan_local_bookmark_plans.append(orphan_plan)

    if not prepared_cleanup.dry_run:
        _apply_stale_cleanup_mutation_plans(
            journal=journal,
            mutation_plans=tuple(mutation_plans),
            orphan_local_bookmark_plans=tuple(orphan_local_bookmark_plans),
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        saver.save_if_changed(next_changes)
    return tuple(prepared_changes)


def _process_stale_cleanup_change(
    *,
    next_changes: dict[str, CachedChange],
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> _StaleCleanupMutationPlan | None:
    stale_reason = prepared_change.stale_reason
    if stale_reason is None:
        return None
    if is_open_pr_record(prepared_change.cached_change):
        pull_request_number = prepared_change.cached_change.pr_number
        assert pull_request_number is not None
        close_hint = ui.cmd(f"jj-stack unstack --cleanup --pull-request {pull_request_number}")
        body = (
            t"preserve open orphan {ui.change_id(prepared_change.change_id)} "
            t"(run {close_hint} to retire it)"
        )
        record_action(
            CleanupAction(
                kind="tracking",
                status="skipped",
                body=body,
            )
        )
        return None

    record_action(
        CleanupAction(
            kind="tracking",
            status="planned" if prepared_cleanup.dry_run else "applied",
            body=t"remove tracking for {ui.change_id(prepared_change.change_id)} "
            t"({stale_reason})",
        )
    )
    if not prepared_cleanup.dry_run:
        next_changes.pop(prepared_change.change_id, None)

    local_bookmark_plan = _plan_local_bookmark_cleanup(
        cleanup_user_bookmarks=prepared_cleanup.context.config.cleanup_user_bookmarks,
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.context.config.bookmark_prefix,
        cached_change=prepared_change.cached_change,
        stale_reason=stale_reason,
    )
    remote_plan = _plan_remote_branch_cleanup(
        cleanup_user_bookmarks=prepared_cleanup.context.config.cleanup_user_bookmarks,
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.context.config.bookmark_prefix,
        cached_change=prepared_change.cached_change,
        local_bookmark_forget_planned=(
            local_bookmark_plan is not None and local_bookmark_plan.status == "planned"
        ),
        remote=prepared_cleanup.remote,
        remote_state=prepared_change.remote_state,
        review_status=prepared_change.review_status,
    )
    if prepared_cleanup.dry_run:
        if local_bookmark_plan is not None:
            record_action(local_bookmark_plan)
        if remote_plan is not None:
            record_action(remote_plan.action)
        return None

    if local_bookmark_plan is not None and local_bookmark_plan.status != "planned":
        record_action(local_bookmark_plan)
    if remote_plan is not None and remote_plan.action.status != "planned":
        record_action(remote_plan.action)

    if (local_bookmark_plan is None or local_bookmark_plan.status != "planned") and (
        remote_plan is None or remote_plan.action.status != "planned"
    ):
        return None

    return _StaleCleanupMutationPlan(
        cached_change=prepared_change.cached_change,
        local_bookmark_action=local_bookmark_plan,
        remote_plan=remote_plan,
    )


def _apply_stale_cleanup_mutation_plans(
    *,
    journal: OperationJournal,
    mutation_plans: tuple[_StaleCleanupMutationPlan, ...],
    orphan_local_bookmark_plans: tuple[OrphanLocalBookmarkCleanupPlan, ...] = (),
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    jj_client = prepared_cleanup.context.jj_client
    remote = prepared_cleanup.remote
    remote_deletions: list[tuple[str, str]] = []
    remote_actions: list[CleanupAction] = []
    local_bookmarks: list[str] = []
    local_actions: list[CleanupAction] = []

    for mutation_plan in mutation_plans:
        remote_plan = mutation_plan.remote_plan
        if (
            remote_plan is not None
            and remote_plan.action.status == "planned"
            and remote is not None
            and remote_plan.expected_remote_target is not None
        ):
            bookmark = mutation_plan.cached_change.bookmark
            if bookmark is None:
                raise AssertionError("Planned remote branch cleanup requires a bookmark.")
            remote_deletions.append((bookmark, remote_plan.expected_remote_target))
            remote_actions.append(remote_plan.action)

        local_bookmark_action = mutation_plan.local_bookmark_action
        if local_bookmark_action is not None and local_bookmark_action.status == "planned":
            bookmark = mutation_plan.cached_change.bookmark
            if bookmark is None:
                raise AssertionError("Planned local bookmark cleanup requires a bookmark.")
            local_bookmarks.append(bookmark)
            local_actions.append(local_bookmark_action)

    for orphan_plan in orphan_local_bookmark_plans:
        if orphan_plan.action.status != "planned":
            continue
        local_bookmarks.append(orphan_plan.bookmark)
        local_actions.append(orphan_plan.action)

    remote_deleted = False
    try:
        if remote_deletions and remote is not None:
            with journal.mutation(
                "delete_remote_bookmarks",
                deletions=tuple(
                    {"bookmark": bookmark, "expected_target": target}
                    for bookmark, target in remote_deletions
                ),
                remote=remote.name,
            ):
                jj_client.delete_remote_bookmarks(
                    remote=remote.name,
                    deletions=tuple(remote_deletions),
                    fetch=False,
                )
                remote_deleted = True
        if local_bookmarks:
            with journal.mutation(
                "forget_local_bookmarks",
                bookmarks=tuple(local_bookmarks),
            ):
                jj_client.forget_bookmarks(tuple(local_bookmarks))
    finally:
        if remote_deleted and remote is not None:
            jj_client.fetch_remote(remote=remote.name)

    for remote_action in remote_actions:
        record_action(replace(remote_action, status="applied"))
    for local_action in local_actions:
        record_action(replace(local_action, status="applied"))


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.remote_context_loaded:
        return prepared_cleanup

    github_target = resolve_github_target(
        prepared_cleanup.context.jj_client.list_git_remotes()
    )

    return replace(
        prepared_cleanup,
        github_repository=github_target.github_repository,
        github_repository_error=github_target.github_repository_error,
        remote=github_target.remote,
        remote_error=github_target.remote_error,
        remote_context_loaded=True,
    )


def _cleanup_needs_remote_context(
    *,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None],
) -> bool:
    """Whether plain cleanup might need remote or GitHub state beyond local checks."""

    for change_id, cached_change in prepared_cleanup.state.changes.items():
        stale_reason = stale_reasons.get(change_id)
        bookmark = cached_change.bookmark
        bookmark_state = prepared_cleanup.bookmark_states.get(
            bookmark or "",
            BookmarkState(name=bookmark or ""),
        )
        if (
            stale_reason is not None
            and bookmark is not None
            and bookmark_state.remote_targets
            and (
                is_review_bookmark(
                    bookmark,
                    prefix=prepared_cleanup.context.config.bookmark_prefix,
                )
                or prepared_cleanup.context.config.cleanup_user_bookmarks
            )
        ):
            return True
        if (
            _stack_comment_cleanup_eligibility(
                cached_change=cached_change,
                stale_reason=stale_reason,
            )
            != "skip"
        ):
            return True
    return False


def _load_bookmark_states(
    *,
    context: CommandContext,
    state: ReviewState,
) -> dict[str, BookmarkState]:
    prefix = context.config.bookmark_prefix
    jj_client = context.jj_client
    bookmark_states = jj_client.list_bookmark_states()
    tracked_bookmarks = {
        cached_change.bookmark
        for cached_change in state.changes.values()
        if cached_change.bookmark is not None
    }
    relevant_bookmarks = {
        bookmark
        for bookmark, bookmark_state in bookmark_states.items()
        if is_review_bookmark(bookmark, prefix=prefix) and bookmark_state.local_targets
    }
    relevant_bookmarks.update(tracked_bookmarks)

    if not relevant_bookmarks:
        return {}

    filtered = {
        bookmark: bookmark_states[bookmark]
        for bookmark in relevant_bookmarks
        if bookmark in bookmark_states
    }
    for bookmark in tracked_bookmarks:
        filtered.setdefault(bookmark, BookmarkState(name=bookmark))
    return filtered


def _remote_cleanup_target(
    remote_state: RemoteBookmarkState | None,
    review_status: ReviewChangeStatus,
) -> str:
    if review_status.remote_branch in {"absent", "conflicted"}:
        raise AssertionError("Cleanup target requires one remote bookmark target.")
    if remote_state is None:
        raise AssertionError("Cleanup target requires remote bookmark state.")
    target = remote_state.target
    if target is None:
        raise AssertionError("Cleanup target requires an unambiguous remote target.")
    return target


def _plan_remote_branch_cleanup(
    *,
    cleanup_user_bookmarks: bool,
    bookmark_state: BookmarkState,
    prefix: str,
    cached_change: CachedChange,
    local_bookmark_forget_planned: bool,
    remote: GitRemote | None,
    remote_state: RemoteBookmarkState | None,
    review_status: ReviewChangeStatus,
) -> RemoteBranchCleanupPlan | None:
    bookmark = cached_change.bookmark
    if bookmark is None:
        return None
    if cached_change.pr_number is None:
        return None
    if not bookmark_cleanup_allowed(
        bookmark=bookmark,
        bookmark_managed=cached_change.manages_bookmark,
        cleanup_user_bookmarks=cleanup_user_bookmarks,
        prefix=prefix,
    ):
        return None
    if remote is None:
        return None

    if review_status.remote_branch == "absent":
        return None

    branch_label = f"{bookmark}@{remote.name}"
    if bookmark_state.local_targets and not local_bookmark_forget_planned:
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                status="blocked",
                body=(
                    t"cannot delete {ui.bookmark(branch_label)} while the local "
                    t"bookmark {ui.bookmark(bookmark)} still exists"
                ),
            ),
        )
    if review_status.remote_branch == "conflicted":
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                status="blocked",
                body=(
                    t"cannot delete {ui.bookmark(branch_label)} because the remote "
                    t"bookmark is conflicted"
                ),
            ),
        )

    return RemoteBranchCleanupPlan(
        action=CleanupAction(
            kind="remote branch",
            status="planned",
            body=t"delete {ui.bookmark(branch_label)}",
        ),
        expected_remote_target=_remote_cleanup_target(remote_state, review_status),
    )


def _plan_local_bookmark_cleanup(
    *,
    cleanup_user_bookmarks: bool,
    bookmark_state: BookmarkState,
    prefix: str,
    cached_change: CachedChange,
    stale_reason: str,
) -> CleanupAction | None:
    bookmark = cached_change.bookmark
    if bookmark is None:
        return None
    if not bookmark_cleanup_allowed(
        bookmark=bookmark,
        bookmark_managed=cached_change.manages_bookmark,
        cleanup_user_bookmarks=cleanup_user_bookmarks,
        prefix=prefix,
    ):
        return None
    match classify_local_bookmark_forget(
        bookmark_state=bookmark_state,
        expected_commit_id=cached_change.last_submitted_commit_id,
    ):
        case "absent":
            return None
        case "conflicted" | "diverged" as safety:
            return CleanupAction(
                kind="local bookmark",
                status="blocked",
                body=local_bookmark_forget_blocked_body(bookmark, safety),
            )
        case _:
            return CleanupAction(
                kind="local bookmark",
                status="planned",
                body=t"forget {ui.bookmark(bookmark)} ({stale_reason})",
            )
