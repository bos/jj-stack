"""Find and remove stale tracking data and review branches left behind by earlier review work.

By default, this runs a repo-wide cleanup of tracking data and review branches that no longer
match an active review.

Open orphaned PRs are left alone. Run `jj-stack list` to see them, then close and clean up one
with `jj-stack unstack --cleanup --pull-request <pr>`, or clean up all of them with
`jj-stack unstack --cleanup --pull-request orphans`.

Use `--dry-run` to preview cleanup without deleting branches, bookmarks, or tracking data.

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
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.github.client import build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.resolution import (
    GithubTarget,
    resolve_github_target,
)
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
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
from jj_stack.state.operation_lock import (
    acquire_operation_lock,
)
from jj_stack.ui import plain_text

from .shared import (
    CleanupAction,
    CleanupResult,
    OrphanLocalBookmarkCleanupPlan,
    PreparedCleanup,
    PreparedCleanupChange,
    RemoteBranchCleanupPlan,
    _build_action_streamer,
    _emit_output_lines,
    _render_cleanup_action_header,
    _render_cleanup_postamble,
    _StaleCleanupMutationPlan,
)
from .stack_comments import (
    _run_stack_comment_cleanup_pass,
    _should_inspect_stack_comment_cleanup,
    _stack_comment_cleanup_eligibility,
)
from .stale import _plan_orphan_local_bookmark_cleanups, _stale_change_reasons

HELP = "Remove stale tracking data and review branches"


def cleanup(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="cleanup",
    ):
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
        change_ids=tuple(prepared_cleanup.state.review_identities),
        context=prepared_cleanup.context,
    )
    if _cleanup_needs_remote_context(
        prepared_cleanup=prepared_cleanup,
        stale_reasons=stale_reasons,
    ):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        for message in github_target_unavailable_messages(prepared_cleanup.github_target):
            console.warning(plain_text(message))

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
        github_target=None,
        dry_run=dry_run,
        state=state,
    )


async def _run_cleanup_async(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None] | None = None,
) -> CleanupResult:
    recorder = ActionRecorder[CleanupAction](on_action=on_action)
    if stale_reasons is None:
        stale_reasons = _stale_change_reasons(
            change_ids=tuple(prepared_cleanup.state.review_identities),
            context=prepared_cleanup.context,
        )
    if _cleanup_needs_remote_context(
        prepared_cleanup=prepared_cleanup,
        stale_reasons=stale_reasons,
    ):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
    prepared_changes = await _run_local_cleanup_pass(
        prepared_cleanup=prepared_cleanup,
        record_action=recorder.record,
        stale_reasons=stale_reasons,
    )
    github_target = prepared_cleanup.github_target
    if isinstance(github_target, GithubTarget) and any(
        prepared_change.inspect_stack_comment for prepared_change in prepared_changes
    ):
        async with build_github_client(repository=github_target.repository) as github_client:
            await _run_stack_comment_cleanup_pass(
                github_client=github_client,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                record_action=recorder.record,
            )
    return CleanupResult(actions=recorder.as_tuple())


async def _run_local_cleanup_pass(
    *,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    stale_reasons: dict[str, str | None],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    mutation_plans: list[_StaleCleanupMutationPlan] = []
    retirements: list[PreparedCleanupChange] = []
    orphan_local_bookmark_plans: list[OrphanLocalBookmarkCleanupPlan] = []
    for change_id, review_identity in prepared_cleanup.state.review_identities.items():
        submitted_baseline = prepared_cleanup.state.submitted_baselines.get(change_id)
        if submitted_baseline is None:
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="skipped",
                    body=t"leave {ui.change_id(change_id)} tracked because its last submitted "
                    t"commit is unavailable; run {ui.cmd('relink')} to repair PR tracking",
                )
            )
            continue
        stale_reason = stale_reasons.get(change_id)
        bookmark_state = prepared_cleanup.bookmark_states.get(
            review_identity.head_ref,
            BookmarkState(name=review_identity.head_ref),
        )
        remote_state = (
            None
            if prepared_cleanup.remote is None
            else bookmark_state.remote_target(prepared_cleanup.remote.name)
        )
        review_status = classify_review_change_without_pull_request(
            commit_id=None,
            local="orphaned",
            remote_state=remote_state,
            review_identity=review_identity,
        )
        prepared_change = PreparedCleanupChange(
            bookmark_state=bookmark_state,
            change_id=change_id,
            inspect_stack_comment=_should_inspect_stack_comment_cleanup(
                remote=prepared_cleanup.remote,
                review_identity=review_identity,
                review_status=review_status,
                stale_reason=stale_reason,
            ),
            remote_state=remote_state,
            review_identity=review_identity,
            review_status=review_status,
            stale_reason=stale_reason,
            submitted_baseline=submitted_baseline,
        )
        prepared_changes.append(prepared_change)
        mutation_plan = _process_stale_cleanup_change(
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        if mutation_plan is not None:
            mutation_plans.append(mutation_plan)
        if stale_reason is not None and not is_open_pr_record(review_identity):
            retirements.append(prepared_change)

    tracked_bookmarks = {
        review_identity.head_ref
        for review_identity in prepared_cleanup.state.review_identities.values()
    }
    for orphan_plan in _plan_orphan_local_bookmark_cleanups(
        bookmark_states=prepared_cleanup.bookmark_states,
        context=prepared_cleanup.context,
        tracked_bookmarks=tracked_bookmarks,
    ):
        if orphan_plan.action.status != "planned":
            record_action(orphan_plan.action)
            continue
        orphan_local_bookmark_plans.append(orphan_plan)

    allowed_mutation_plans, blocked_change_ids = await _guard_remote_cleanup_plans(
        mutation_plans=tuple(mutation_plans),
        prepared_cleanup=prepared_cleanup,
        record_action=record_action,
    )
    if prepared_cleanup.dry_run:
        allowed_by_change = {plan.change_id: plan for plan in allowed_mutation_plans}
        for retirement in retirements:
            if retirement.change_id in blocked_change_ids:
                continue
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="planned",
                    body=t"remove tracking for {ui.change_id(retirement.change_id)} "
                    t"({retirement.stale_reason})",
                )
            )
            plan = allowed_by_change.get(retirement.change_id)
            if plan is not None:
                for action in (
                    plan.local_bookmark_action,
                    plan.remote_plan.action if plan.remote_plan is not None else None,
                ):
                    if action is not None and action.status == "planned":
                        record_action(action)
        for orphan_plan in orphan_local_bookmark_plans:
            record_action(orphan_plan.action)
    else:
        _apply_stale_cleanup_mutation_plans(
            mutation_plans=allowed_mutation_plans,
            orphan_local_bookmark_plans=tuple(orphan_local_bookmark_plans),
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        for retirement in retirements:
            if retirement.change_id in blocked_change_ids:
                continue
            prepared_cleanup.context.state_store.retire_review(
                retirement.change_id,
                expected_identity=retirement.review_identity,
                expected_baseline=retirement.submitted_baseline,
            )
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="applied",
                    body=t"remove tracking for {ui.change_id(retirement.change_id)} "
                    t"({retirement.stale_reason})",
                )
            )
    return tuple(prepared_changes)


async def _guard_remote_cleanup_plans(
    *,
    mutation_plans: tuple[_StaleCleanupMutationPlan, ...],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> tuple[tuple[_StaleCleanupMutationPlan, ...], set[str]]:
    """Exclude remote cleanup that conflicts with live GitHub ownership."""

    target = prepared_cleanup.github_target
    candidates = tuple(
        plan
        for plan in mutation_plans
        if plan.remote_plan is not None and plan.remote_plan.action.status == "planned"
    )
    if not candidates:
        return mutation_plans, set()
    if not isinstance(target, GithubTarget):
        blocked_change_ids = {plan.change_id for plan in candidates}
        for plan in candidates:
            record_action(
                CleanupAction(
                    kind="remote branch",
                    status="blocked",
                    body=t"preserve PR #{plan.review_identity.pr_number}'s branch because the "
                    t"GitHub repository cannot be resolved; fix the remote and retry cleanup",
                )
            )
        return (
            tuple(plan for plan in mutation_plans if plan.change_id not in blocked_change_ids),
            blocked_change_ids,
        )
    pull_numbers = tuple(plan.review_identity.pr_number for plan in candidates)
    async with build_github_client(repository=target.repository) as github_client:
        stacks = await GithubStackSelection(
            github_client,
            pull_numbers,
            prepared_cleanup.context.state_store,
        ).overlapping(persist=not prepared_cleanup.dry_run)
    stack_by_pull = {
        pull_number: stack.number
        for stack in stacks
        for pull_number in stack.active_pull_request_numbers
        if pull_number in pull_numbers
    }
    blocked_change_ids: set[str] = set()
    for plan in candidates:
        pull_number = plan.review_identity.pr_number
        stack_number = stack_by_pull.get(pull_number)
        if stack_number is None:
            continue
        blocked_change_ids.add(plan.change_id)
        blocker = (
            t"it remains in GitHub stack #{stack_number}; run "
            t"{ui.cmd(f'gh stack unstack {stack_number}')} and retry cleanup"
        )
        record_action(
            CleanupAction(
                kind="remote branch",
                status="blocked",
                body=t"preserve PR #{pull_number}'s branch because {blocker}",
            )
        )
    return (
        tuple(plan for plan in mutation_plans if plan.change_id not in blocked_change_ids),
        blocked_change_ids,
    )


def _process_stale_cleanup_change(
    *,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> _StaleCleanupMutationPlan | None:
    stale_reason = prepared_change.stale_reason
    if stale_reason is None:
        return None
    review_identity = prepared_change.review_identity
    if is_open_pr_record(review_identity):
        close_hint = ui.cmd("jj-stack unstack --cleanup --pull-request orphans")
        body = (
            t"preserve open orphan {ui.change_id(prepared_change.change_id)} "
            t"(run {close_hint} to close and clean up all open orphans)"
        )
        record_action(
            CleanupAction(
                kind="tracking",
                status="skipped",
                body=body,
            )
        )
        return None

    local_bookmark_plan = _plan_local_bookmark_cleanup(
        cleanup_user_bookmarks=prepared_cleanup.context.config.cleanup_user_bookmarks,
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.context.config.bookmark_prefix,
        review_identity=review_identity,
        submitted_baseline=prepared_change.submitted_baseline,
        stale_reason=stale_reason,
    )
    remote_plan = _plan_remote_branch_cleanup(
        cleanup_user_bookmarks=prepared_cleanup.context.config.cleanup_user_bookmarks,
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.context.config.bookmark_prefix,
        review_identity=review_identity,
        local_bookmark_forget_planned=(
            local_bookmark_plan is not None and local_bookmark_plan.status == "planned"
        ),
        remote=prepared_cleanup.remote,
        remote_state=prepared_change.remote_state,
        review_status=prepared_change.review_status,
    )
    if local_bookmark_plan is not None and local_bookmark_plan.status != "planned":
        record_action(local_bookmark_plan)
    if remote_plan is not None and remote_plan.action.status != "planned":
        record_action(remote_plan.action)

    if (local_bookmark_plan is None or local_bookmark_plan.status != "planned") and (
        remote_plan is None or remote_plan.action.status != "planned"
    ):
        return None

    return _StaleCleanupMutationPlan(
        change_id=prepared_change.change_id,
        local_bookmark_action=local_bookmark_plan,
        remote_plan=remote_plan,
        review_identity=review_identity,
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
            bookmark = mutation_plan.review_identity.head_ref
            remote_deletions.append((bookmark, remote_plan.expected_remote_target))
            remote_actions.append(remote_plan.action)

        local_bookmark_action = mutation_plan.local_bookmark_action
        if local_bookmark_action is not None and local_bookmark_action.status == "planned":
            bookmark = mutation_plan.review_identity.head_ref
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


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.github_target is not None:
        return prepared_cleanup
    return replace(
        prepared_cleanup,
        github_target=resolve_github_target(
            prepared_cleanup.context.jj_client.list_git_remotes()
        ),
    )


def _cleanup_needs_remote_context(
    *,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None],
) -> bool:
    """Whether plain cleanup might need remote or GitHub state beyond local checks."""

    for change_id, review_identity in prepared_cleanup.state.review_identities.items():
        if change_id not in prepared_cleanup.state.submitted_baselines:
            continue
        stale_reason = stale_reasons.get(change_id)
        bookmark = review_identity.head_ref
        bookmark_state = prepared_cleanup.bookmark_states.get(
            bookmark,
            BookmarkState(name=bookmark),
        )
        if (
            stale_reason is not None
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
                review_identity=review_identity,
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
        review_identity.head_ref
        for change_id, review_identity in state.review_identities.items()
        if change_id in state.submitted_baselines
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
    review_identity: ReviewIdentity,
    local_bookmark_forget_planned: bool,
    remote: GitRemote | None,
    remote_state: RemoteBookmarkState | None,
    review_status: ReviewChangeStatus,
) -> RemoteBranchCleanupPlan | None:
    bookmark = review_identity.head_ref
    if not bookmark_cleanup_allowed(
        bookmark=bookmark,
        bookmark_managed=review_identity.manages_bookmark,
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
    review_identity: ReviewIdentity,
    stale_reason: str,
    submitted_baseline: SubmittedBaseline,
) -> CleanupAction | None:
    bookmark = review_identity.head_ref
    if not bookmark_cleanup_allowed(
        bookmark=bookmark,
        bookmark_managed=review_identity.manages_bookmark,
        cleanup_user_bookmarks=cleanup_user_bookmarks,
        prefix=prefix,
    ):
        return None
    match classify_local_bookmark_forget(
        bookmark_state=bookmark_state,
        expected_commit_id=submitted_baseline.commit_id,
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
