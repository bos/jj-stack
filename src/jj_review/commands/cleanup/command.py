"""Find and remove stale tracking data and review branches left behind by earlier review work.

By default, this runs a repo-wide cleanup of tracking data and review branches that no longer
match an active review. With `--rebase [REVSET]`, it works on one local stack instead.

Open orphaned PRs are preserved. Run `jj-review list` to see them, then retire one explicitly
with `jj-review close --cleanup --pull-request <pr>`.

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

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.commands._close_actions import comment_matches_kind as _comment_matches_kind
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    select_submit_remote,
)
from jj_review.github.stack_comments import (
    StackCommentKind,
    stack_comment_label,
)
from jj_review.jj import JjCliArgs
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubIssueComment
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.bookmarks import is_review_bookmark
from jj_review.review.change_status import (
    ReviewChangeStatus,
    classify_review_change,
    is_open_pr_record,
)
from jj_review.state.journal import (
    OperationJournal,
)
from jj_review.state.operation_lock import (
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
    StackCommentCleanupEligibility,
    StackCommentCleanupPlan,
    _build_action_streamer,
    _CleanupActionRecorder,
    _emit_output_lines,
    _emit_severity_lines,
    _render_cleanup_action_header,
    _render_cleanup_postamble,
    _render_remote_and_github_lines,
    _StaleCleanupMutationPlan,
)

HELP = "Remove stale tracking data and review branches; optionally rebase one stack"
_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY


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
                github_repository=(
                    prepared_cleanup.github_repository.full_name
                    if prepared_cleanup.github_repository is not None
                    else None
                ),
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
    return 0


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
    recorder = _CleanupActionRecorder(on_action=on_action)
    dry_run = prepared_cleanup.dry_run

    # Write an operation journal before the first mutation on live runs only.
    journal: OperationJournal | None = None
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
        prepared_changes = _run_local_cleanup_pass(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=recorder.record,
            stale_reasons=stale_reasons,
        )
        if prepared_cleanup.github_repository is None:
            _save_cleanup_state_if_changed(
                next_changes=next_changes,
                prepared_cleanup=prepared_cleanup,
            )

            _cleanup_succeeded = True
            return CleanupResult(
                actions=recorder.as_tuple(),
            )

        if not any(prepared_change.inspect_stack_comment for prepared_change in prepared_changes):
            _save_cleanup_state_if_changed(
                next_changes=next_changes,
                prepared_cleanup=prepared_cleanup,
            )

            _cleanup_succeeded = True
            return CleanupResult(
                actions=recorder.as_tuple(),
            )

        github_repository = prepared_cleanup.github_repository
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            await _run_stack_comment_cleanup_pass(
                github_client=github_client,
                github_repository=github_repository,
                next_changes=next_changes,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                record_action=recorder.record,
            )

        _save_cleanup_state_if_changed(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
        )

        _cleanup_succeeded = True
        return CleanupResult(
            actions=recorder.as_tuple(),
        )
    finally:
        if _cleanup_succeeded and journal is not None:
            journal.append(
                "completed",
                {"cached_change_ids": tuple(prepared_cleanup.state.changes)},
            )


def _run_local_cleanup_pass(
    *,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
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
        review_status = _classify_cleanup_change(
            cached_change=cached_change,
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
    for bookmark, bookmark_state in sorted(prepared_cleanup.bookmark_states.items()):
        if bookmark in tracked_bookmarks or not is_review_bookmark(
            bookmark,
            prefix=prepared_cleanup.context.config.bookmark_prefix,
        ):
            continue
        orphan_plan = _plan_orphan_local_bookmark_cleanup(
            bookmark_state=bookmark_state,
            context=prepared_cleanup.context,
        )
        if orphan_plan is None:
            continue
        if prepared_cleanup.dry_run:
            record_action(orphan_plan.action)
            continue
        if orphan_plan.action.status != "planned":
            record_action(orphan_plan.action)
            continue
        orphan_local_bookmark_plans.append(orphan_plan)

    if not prepared_cleanup.dry_run:
        _apply_stale_cleanup_mutation_plans(
            mutation_plans=tuple(mutation_plans),
            orphan_local_bookmark_plans=tuple(orphan_local_bookmark_plans),
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        _save_cleanup_state_if_changed(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
        )
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
        close_hint = ui.cmd(f"jj-review close --cleanup --pull-request {pull_request_number}")
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
            jj_client.delete_remote_bookmarks(
                remote=remote.name,
                deletions=tuple(remote_deletions),
                fetch=False,
            )
            remote_deleted = True
        if local_bookmarks:
            jj_client.forget_bookmarks(tuple(local_bookmarks))
    finally:
        if remote_deleted and remote is not None:
            jj_client.fetch_remote(remote=remote.name)

    for remote_action in remote_actions:
        record_action(replace(remote_action, status="applied"))
    for local_action in local_actions:
        record_action(replace(local_action, status="applied"))


def _save_cleanup_state_if_changed(
    *,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
) -> None:
    if not prepared_cleanup.dry_run and next_changes != prepared_cleanup.state.changes:
        prepared_cleanup.context.state_store.save(
            prepared_cleanup.state.model_copy(update={"changes": dict(next_changes)})
        )


async def _run_stack_comment_cleanup_pass(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    next_changes: dict[str, CachedChange],
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    stack_comment_changes = tuple(
        prepared_change
        for prepared_change in prepared_changes
        if prepared_change.inspect_stack_comment
    )
    with console.progress(
        description="Inspecting stack comments",
        total=len(stack_comment_changes),
    ) as progress:
        comment_plans = await run_bounded_tasks(
            concurrency=_GITHUB_INSPECTION_CONCURRENCY,
            items=stack_comment_changes,
            run_item=lambda prepared_change: _plan_stack_comment_cleanup(
                cached_change=prepared_change.cached_change,
                bookmark_state=prepared_change.bookmark_state,
                github_client=github_client,
                github_repository=github_repository,
            ),
            on_success=lambda _index, _result: progress.advance(),
        )
    for prepared_change, comment_plan in zip(
        stack_comment_changes,
        comment_plans,
        strict=True,
    ):
        if comment_plan is None:
            continue
        await _apply_stack_comment_cleanup_action(
            comment_plan=comment_plan,
            change_id=prepared_change.change_id,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )


async def _apply_stack_comment_cleanup_action(
    *,
    comment_plan: StackCommentCleanupPlan,
    change_id: str,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    applied_comments = False
    targeted_actions = comment_plan.actions[: len(comment_plan.comments)]
    for action, (comment_id, kind) in zip(
        targeted_actions,
        comment_plan.comments,
        strict=True,
    ):
        comment_action = action
        if not prepared_cleanup.dry_run and comment_action.status == "planned":
            try:
                await github_client.delete_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=comment_id,
                )
            except GithubClientError as error:
                raise CliError(
                    f"Could not delete {stack_comment_label(kind)} #{comment_id}"
                ) from error
            applied_comments = True
            comment_action = replace(action, status="applied")
        record_action(comment_action)
    for action in comment_plan.actions[len(targeted_actions) :]:
        record_action(action)
    if applied_comments and change_id in next_changes:
        next_changes[change_id] = next_changes[change_id].with_cleared_comments()
    if not prepared_cleanup.dry_run:
        prepared_cleanup.context.state_store.save(
            prepared_cleanup.state.model_copy(update={"changes": dict(next_changes)})
        )


def _resolve_remote(*, context: CommandContext) -> tuple[GitRemote | None, ErrorMessage | None]:
    try:
        return select_submit_remote(context.jj_client.list_git_remotes()), None
    except CliError as error:
        return None, error_message(error)


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.remote_context_loaded:
        return prepared_cleanup

    remote, remote_error = _resolve_remote(context=prepared_cleanup.context)
    github_repository = None
    github_error = None
    if remote is not None:
        github_repository = parse_github_repo(remote)
        if github_repository is None:
            github_error = (
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(remote.name)}. Use a GitHub remote URL."
            )

    return replace(
        prepared_cleanup,
        github_repository=github_repository,
        github_repository_error=github_error,
        remote=remote,
        remote_error=remote_error,
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


def _stack_comment_cleanup_eligibility(
    *,
    cached_change: CachedChange,
    stale_reason: str | None,
) -> StackCommentCleanupEligibility:
    """Classify whether cleanup can inspect stack comments for this change."""

    if cached_change.pr_number is None:
        if cached_change.is_unlinked and cached_change.bookmark is not None:
            return "inspect"
        return "skip"
    if cached_change.is_unlinked:
        return "inspect"
    if (
        cached_change.bookmark is None
        and cached_change.navigation_comment_id is None
        and cached_change.overview_comment_id is None
    ):
        return "skip"
    if stale_reason is None:
        return "inspect"
    if cached_change.pr_state in {"closed", "merged"}:
        return "skip"
    if (
        cached_change.navigation_comment_id is not None
        or cached_change.overview_comment_id is not None
    ):
        return "inspect"
    if cached_change.bookmark is None:
        return "skip"
    return "needs-remote-check"


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


def _stale_change_reasons(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
) -> dict[str, str | None]:
    jj_client = context.jj_client
    matched_revisions = jj_client.query_revisions_by_change_ids(change_ids)
    reasons: dict[str, str | None] = {}

    for change_id in change_ids:
        revisions = matched_revisions.get(change_id, ())
        if not revisions:
            reasons[change_id] = "no visible local change matches that cached change ID"
            continue
        if len(revisions) > 1:
            reasons[change_id] = "multiple visible revisions still share that change ID"
            continue

        revision = revisions[0]
        if not revision.is_reviewable():
            reasons[change_id] = "local change is no longer reviewable"
            continue

        reasons[change_id] = None

    candidate_revisions = tuple(
        revisions[0]
        for change_id in change_ids
        if reasons.get(change_id) is None
        for revisions in (matched_revisions.get(change_id, ()),)
        if revisions
    )
    supported_change_ids = jj_client.supported_review_stack_change_ids(candidate_revisions)
    for revision in candidate_revisions:
        if revision.change_id not in supported_change_ids:
            reasons[revision.change_id] = (
                "local change no longer participates in a supported review stack"
            )
    return reasons


def _classify_cleanup_change(
    *,
    cached_change: CachedChange,
    remote_state: RemoteBookmarkState | None,
) -> ReviewChangeStatus:
    return classify_review_change(
        cached_change=cached_change,
        commit_id=None,
        local="orphaned",
        pull_request_lookup=None,
        remote_state=remote_state,
    )


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
    if cached_change.manages_bookmark:
        if not is_review_bookmark(bookmark, prefix=prefix):
            return None
    elif not cleanup_user_bookmarks:
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
    if cached_change.manages_bookmark:
        if not is_review_bookmark(bookmark, prefix=prefix):
            return None
    elif not cleanup_user_bookmarks:
        return None
    if not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return CleanupAction(
            kind="local bookmark",
            status="blocked",
            body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
        )

    local_target = bookmark_state.local_target
    if local_target is None:
        return None

    expected_target = cached_change.last_submitted_commit_id
    if expected_target is not None and local_target != expected_target:
        return CleanupAction(
            kind="local bookmark",
            status="blocked",
            body=(
                t"cannot forget {ui.bookmark(bookmark)} because it already points "
                t"to a different revision"
            ),
        )

    return CleanupAction(
        kind="local bookmark",
        status="planned",
        body=t"forget {ui.bookmark(bookmark)} ({stale_reason})",
    )


def _plan_orphan_local_bookmark_cleanup(
    *,
    bookmark_state: BookmarkState,
    context: CommandContext,
) -> OrphanLocalBookmarkCleanupPlan | None:
    bookmark = bookmark_state.name
    prefix = context.config.bookmark_prefix
    jj_client = context.jj_client
    if not is_review_bookmark(bookmark, prefix=prefix) or not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return OrphanLocalBookmarkCleanupPlan(
            bookmark=bookmark,
            action=CleanupAction(
                kind="local bookmark",
                status="blocked",
                body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
            ),
        )

    local_target = bookmark_state.local_target
    if local_target is None:
        return None

    revisions = jj_client.query_revisions(local_target)
    if not revisions:
        stale_reason = "target is no longer visible locally"
    else:
        revision = revisions[0]
        if not revision.is_reviewable():
            stale_reason = "target is no longer reviewable"
        elif revision.change_id not in jj_client.supported_review_stack_change_ids((revision,)):
            stale_reason = "target no longer participates in a supported review stack"
        else:
            return None

    return OrphanLocalBookmarkCleanupPlan(
        bookmark=bookmark,
        action=CleanupAction(
            kind="local bookmark",
            status="planned",
            body=t"forget {ui.bookmark(bookmark)} ({stale_reason})",
        ),
    )


def _should_inspect_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    remote: GitRemote | None,
    review_status: ReviewChangeStatus,
    stale_reason: str | None,
) -> bool:
    eligibility = _stack_comment_cleanup_eligibility(
        cached_change=cached_change,
        stale_reason=stale_reason,
    )
    if eligibility == "inspect":
        return True
    if eligibility == "skip":
        return False
    if remote is None:
        return False
    return review_status.remote_branch == "absent"


async def _plan_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
    github_repository,
) -> StackCommentCleanupPlan | None:
    pull_request_number = cached_change.pr_number
    if pull_request_number is None and cached_change.is_unlinked:
        pull_request_number = await _resolve_unlinked_pull_request_number(
            bookmark_state=bookmark_state,
            github_client=github_client,
            github_repository=github_repository,
        )
        if isinstance(pull_request_number, CleanupAction):
            return StackCommentCleanupPlan(actions=(pull_request_number,))

    if pull_request_number is None:
        return None

    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CliError(f"Could not load pull request #{pull_request_number}") from error

    if not cached_change.is_unlinked:
        bookmark = cached_change.bookmark
        if bookmark is None:
            return None
        expected_label = f"{github_repository.owner}:{bookmark}"
        if pull_request.head.ref == bookmark and pull_request.head.label == expected_label:
            return None

    managed_comments = await _resolve_managed_comments(
        cached_change=cached_change,
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=pull_request_number,
    )
    if isinstance(managed_comments, CleanupAction):
        return StackCommentCleanupPlan(actions=(managed_comments,))
    if not managed_comments:
        return None

    return StackCommentCleanupPlan(
        actions=tuple(
            CleanupAction(
                kind=stack_comment_label(kind),
                status="planned",
                body=(
                    f"delete {stack_comment_label(kind)} #{comment.id} from PR "
                    f"#{pull_request_number}"
                ),
            )
            for kind, comment in managed_comments
        ),
        comments=tuple((comment.id, kind) for kind, comment in managed_comments),
    )


async def _resolve_managed_comments(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> tuple[tuple[StackCommentKind, GithubIssueComment], ...] | CleanupAction:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not list stack comments for pull request #{pull_request_number}"
        ) from error

    resolved: list[tuple[StackCommentKind, GithubIssueComment]] = []
    for kind, cached_comment_id in (
        ("navigation", cached_change.navigation_comment_id),
        ("overview", cached_change.overview_comment_id),
    ):
        cached_comment = None
        if cached_comment_id is not None:
            cached_comment = next(
                (comment for comment in comments if comment.id == cached_comment_id),
                None,
            )
            if cached_comment is not None and not _comment_matches_kind(
                body=cached_comment.body,
                kind=kind,
            ):
                return CleanupAction(
                    kind=stack_comment_label(kind),
                    status="blocked",
                    body=(
                        f"cannot delete saved {stack_comment_label(kind)} "
                        f"#{cached_comment.id} because it does not belong to us"
                    ),
                )
        if cached_comment is not None:
            resolved.append((kind, cached_comment))
            continue

        matching_comments = [
            comment for comment in comments if _comment_matches_kind(body=comment.body, kind=kind)
        ]
        if len(matching_comments) > 1:
            return CleanupAction(
                kind=stack_comment_label(kind),
                status="blocked",
                body=(
                    f"cannot delete {stack_comment_label(kind)}s because GitHub reports "
                    f"multiple candidates on PR #{pull_request_number}"
                ),
            )
        if matching_comments:
            resolved.append((kind, matching_comments[0]))

    return tuple(resolved)


async def _resolve_unlinked_pull_request_number(
    *,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
    github_repository,
) -> int | CleanupAction | None:
    if bookmark_state.name == "":
        return None

    try:
        pull_requests = await github_client.list_pull_requests(
            github_repository.owner,
            github_repository.repo,
            head=f"{github_repository.owner}:{bookmark_state.name}",
            state="all",
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not list pull requests for unlinked bookmark "
            t"{ui.bookmark(bookmark_state.name)}"
        ) from error

    if not pull_requests:
        return None
    if len(pull_requests) > 1:
        return CleanupAction(
            kind="stack navigation comment",
            status="blocked",
            body=(
                t"cannot delete stack navigation comments because GitHub reports multiple "
                t"pull requests for unlinked bookmark {ui.bookmark(bookmark_state.name)}"
            ),
        )
    return pull_requests[0].number
