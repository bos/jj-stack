"""Orphan-close path for `close --cleanup --pull-request <pr>`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from jj_review import console, ui
from jj_review.commands._close_actions import (
    BookmarkCleanupPlan as _OrphanBookmarkCleanupPlan,
    CloseAction,
    apply_bookmark_cleanup,
    close_action_presentation,
    find_managed_comment,
    plan_bookmark_cleanup,
    render_close_action_message,
    retire_cached_change as _retire_cached_change,
)
from jj_review.config import RepoConfig
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.error_messages import (
    github_unavailable_message,
    remote_unavailable_message,
    summarize_github_error_reason,
)
from jj_review.github.resolution import (
    ParsedGithubRepo,
    select_submit_remote,
)
from jj_review.github.stack_comments import StackCommentKind, stack_comment_label
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.github import GithubIssueComment, GithubPullRequest
from jj_review.models.intent import LoadedIntent, SubmitIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.bookmarks import find_changes_by_bookmark, is_review_bookmark
from jj_review.review.intents import close_intent_mode_relation
from jj_review.state.journal import (
    CloseOperationRecord,
    LoadedOperationRecord,
    OperationJournal,
    append_abandoned_event,
)
from jj_review.state.operation_lock import OperationLock, read_operation_lock_holder
from jj_review.state.store import ReviewStateStore
from jj_review.system import pid_is_alive
from jj_review.ui import Message, plain_text

OrphanedPullRequestState = Literal["closed", "open"]
GithubClientBuilder = Callable[..., Any]
GithubRepoParser = Callable[[GitRemote], ParsedGithubRepo | None]
ReportStaleCloseOperations = Callable[..., None]
RetireSubmitIntentsClearedByCleanup = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _OrphanedPullRequestInspection:
    """Resolved GitHub view of one orphaned tracked pull request."""

    pull_request: GithubPullRequest
    state: OrphanedPullRequestState


@dataclass(frozen=True, slots=True)
class _ResolvedOrphanedComment:
    """One managed stack comment proven safe to delete during orphan cleanup."""

    comment: GithubIssueComment
    kind: StackCommentKind


@dataclass(slots=True)
class _OrphanActionRecorder:
    """Collect orphan-close actions and track whether any step blocked progress."""

    actions: list[CloseAction]
    blocked: bool = False

    def record(self, action: CloseAction) -> None:
        if action.status == "blocked":
            self.blocked = True
        self.actions.append(action)

    def as_tuple(self) -> tuple[CloseAction, ...]:
        return tuple(self.actions)


@dataclass(frozen=True, slots=True)
class _OrphanCloseOperationState:
    """Prepared close operation bookkeeping for resumable orphan cleanup runs."""

    journal: OperationJournal | None
    stale_close_operations: list[LoadedOperationRecord]
    stale_submit_intents: list[LoadedIntent]



def state_has_pull_request_record(
    *,
    pull_request_number: int,
    state: ReviewState,
) -> bool:
    return any(
        cached_change.link_state == "active"
        and cached_change.pr_number == pull_request_number
        for cached_change in state.changes.values()
    )


async def run_untracked_cleanup_pull_request(
    *,
    dry_run: bool,
    github_client_builder: GithubClientBuilder,
    github_repo_parser: GithubRepoParser,
    jj_client: JjClient,
    pull_request_number: int,
    retire_submit_intents_cleared_by_cleanup: RetireSubmitIntentsClearedByCleanup,
    state: ReviewState,
    state_store: ReviewStateStore,
) -> int:
    """Handle cleanup by PR number after saved tracking was already retired."""

    remotes = jj_client.list_git_remotes()
    if not remotes:
        raise _untracked_cleanup_verification_error(
            detail="no GitHub remote is configured",
            pull_request_number=pull_request_number,
        )
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        raise _untracked_cleanup_verification_error(
            detail=plain_text(error_message(error)),
            pull_request_number=pull_request_number,
        ) from error

    github_repository = github_repo_parser(remote)
    if github_repository is None:
        raise _untracked_cleanup_verification_error(
            detail=f"remote {remote.name} is not a GitHub remote",
            pull_request_number=pull_request_number,
        )

    async with github_client_builder(base_url=github_repository.api_base_url) as github_client:
        try:
            pull_request = await github_client.get_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request_number,
            )
        except GithubClientError as error:
            raise _untracked_cleanup_verification_error(
                detail=summarize_github_error_reason(error),
                pull_request_number=pull_request_number,
            ) from error

    if pull_request.state != "closed":
        raise CliError(
            t"PR #{pull_request_number} is not tracked locally, and it is still open.",
            hint=(
                t"Run {ui.cmd('import')} or {ui.cmd('relink')} if this PR should be "
                t"attached locally, or close it on GitHub."
            ),
        )

    if not dry_run:
        _retire_pull_request_close_intents(
            pull_request_number=pull_request_number,
            state_store=state_store,
        )
        retire_submit_intents_cleared_by_cleanup(
            current_state=state,
            jj_client=jj_client,
            stale_submit_intents=[
                loaded
                for loaded in state_store.list_intents()
                if isinstance(loaded.intent, SubmitIntent)
                and not pid_is_alive(loaded.intent.pid)
            ],
        )
    console.output(t"Nothing to close for PR #{pull_request_number}.")
    return 0


def _untracked_cleanup_verification_error(
    *,
    detail: str,
    pull_request_number: int,
) -> CliError:
    return CliError(
        t"Could not verify whether PR #{pull_request_number} was already cleaned up.",
        hint=(
            t"{detail}. Restore GitHub access and retry "
            t"{ui.cmd(f'close --cleanup --pull-request {pull_request_number}')}."
        ),
    )


def _retire_pull_request_close_intents(
    *,
    pull_request_number: int,
    state_store: ReviewStateStore,
) -> None:
    """Retire stale close journals recorded for a now-closed PR selector."""

    display_revset = f"--pull-request {pull_request_number}"
    state_store.require_writable()
    stale_close_operations: list[tuple[LoadedOperationRecord, CloseOperationRecord]] = []
    for loaded in state_store.list_operations():
        operation = loaded.operation
        if isinstance(operation, CloseOperationRecord):
            stale_close_operations.append((loaded, operation))
    pr_selector_operations = [
        operation
        for _loaded, operation in stale_close_operations
        if operation.display_revset == display_revset
    ]
    covered_change_ids = {
        change_id
        for operation in pr_selector_operations
        for change_id in operation.ordered_change_ids
    }
    for loaded, operation in stale_close_operations:
        recorded_change_ids = set(operation.ordered_change_ids)
        matches_pr_selector = operation.display_revset == display_revset
        covered_by_pr_selector = (
            bool(recorded_change_ids)
            and recorded_change_ids.issubset(covered_change_ids)
            and close_intent_mode_relation(
                recorded_cleanup=operation.cleanup,
                current_cleanup=True,
            )
            != "incompatible"
        )
        if not matches_pr_selector and not covered_by_pr_selector:
            continue
        append_abandoned_event(loaded.path, reason="superseded_by_close")


async def run_orphan_close(
    *,
    change_id: str,
    config: RepoConfig,
    dry_run: bool,
    github_client_builder: GithubClientBuilder,
    github_repo_parser: GithubRepoParser,
    jj_client: JjClient,
    operation_lock: OperationLock,
    pull_request_number: int,
    report_stale_close_operations: ReportStaleCloseOperations,
    retire_submit_intents_cleared_by_cleanup: RetireSubmitIntentsClearedByCleanup,
    state: ReviewState,
    state_store: ReviewStateStore,
) -> int:
    """Close an orphaned PR, deleting its review artifacts via saved data."""

    cached_change = state.changes.get(change_id)
    if cached_change is None:
        raise CliError(t"PR #{pull_request_number} is no longer tracked locally.")
    bookmark = cached_change.bookmark
    if bookmark is None:
        raise CliError(
            t"PR #{pull_request_number} has no saved bookmark; cannot clean up orphaned branch.",
            hint=t"Run {ui.cmd('unlink')} to detach the saved record manually.",
        )
    other_claimants = tuple(
        other_change_id
        for other_change_id in find_changes_by_bookmark(state, bookmark)
        if other_change_id != change_id
    )
    if other_claimants:
        rendered_others = ", ".join(other[:8] for other in other_claimants)
        raise CliError(
            t"Bookmark {ui.bookmark(bookmark)} is now claimed by another tracked change "
            t"({rendered_others}); refusing to delete the branch from under a live review.",
            hint=t"Run {ui.cmd('unlink')} on the orphan record instead.",
        )

    remotes = jj_client.list_git_remotes()
    remote_error: ErrorMessage | None = None
    remote: GitRemote | None = None
    try:
        remote = select_submit_remote(remotes) if remotes else None
    except CliError as error:
        remote_error = error_message(error)
    github_repository = github_repo_parser(remote) if remote is not None else None
    github_error: ErrorMessage | None = None
    if remote is not None and github_repository is None:
        github_error = f"Could not determine the GitHub repository for remote {remote.name}."
    if remote is None or github_repository is None:
        if remote is None:
            console.warning(remote_unavailable_message(remote_error=remote_error))
        github_message = github_unavailable_message(
            github_error=github_error,
            github_repository=None,
        )
        if github_message is not None:
            console.warning(github_message)
        return 1

    label = ui.change_id(change_id)
    revision_label = t"orphaned change {label}"
    last_target = cached_change.last_submitted_commit_id
    cleanup_bookmark = _orphan_should_cleanup_bookmark(
        bookmark=bookmark,
        cached_change=cached_change,
        cleanup_user_bookmarks=config.cleanup_user_bookmarks,
        prefix=config.bookmark_prefix,
    )
    if cleanup_bookmark:
        jj_client.fetch_remote(remote=remote.name, branches=(bookmark,))
    bookmark_state = jj_client.get_bookmark_state(bookmark)
    recorder = _OrphanActionRecorder(actions=[])
    completed = False
    final_state: ReviewState | None = None
    operation_state = _OrphanCloseOperationState(
        journal=None,
        stale_close_operations=[],
        stale_submit_intents=[],
    )
    try:
        operation_state = _start_orphan_close_operation(
            cached_change=cached_change,
            change_id=change_id,
            dry_run=dry_run,
            operation_lock=operation_lock,
            pull_request_number=pull_request_number,
            report_stale_close_operations=report_stale_close_operations,
            state_store=state_store,
        )

        async with github_client_builder(
            base_url=github_repository.api_base_url
        ) as github_client:
            inspection, blocked_action = await _lookup_orphaned_pull_request(
                cached_change=cached_change,
                github_client=github_client,
                github_repository=github_repository,
                pull_request_number=pull_request_number,
            )
            if blocked_action is not None:
                recorder.record(blocked_action)

            cleanup_plan = _OrphanBookmarkCleanupPlan(
                local_forget=False,
                remote_delete=False,
            )
            resolved_comments: tuple[_ResolvedOrphanedComment, ...] = ()
            if not recorder.blocked and cleanup_bookmark:
                cleanup_plan = _preflight_orphan_bookmark_cleanup(
                    bookmark=bookmark,
                    bookmark_state=bookmark_state,
                    cached_change=cached_change,
                    cleanup_user_bookmarks=config.cleanup_user_bookmarks,
                    dry_run=dry_run,
                    prefix=config.bookmark_prefix,
                    recorder=recorder,
                    remote_name=remote.name,
                    saved_commit_id=last_target,
                )
            if not recorder.blocked:
                resolved_comments = await _preflight_orphaned_comment_cleanup(
                    cached_change=cached_change,
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request_number=pull_request_number,
                    recorder=recorder,
                )
            if recorder.blocked:
                _retire_blocked_orphan_close_tracking(
                    cached_change=cached_change,
                    change_id=change_id,
                    dry_run=dry_run,
                    inspection=inspection,
                    recorder=recorder,
                    revision_label=revision_label,
                    state=state,
                    state_store=state_store,
                )
                completed = True
                return _render_orphan_close_actions(
                    actions=recorder.as_tuple(),
                    blocked=True,
                    dry_run=dry_run,
                )

            if inspection is None:
                raise AssertionError("Orphan close inspection must resolve a pull request state.")
            if inspection.state == "open":
                recorder.record(
                    CloseAction(
                        kind="pull request",
                        body=t"close PR #{pull_request_number} for orphaned change {label}",
                        status="planned" if dry_run else "applied",
                    )
                )
                if not dry_run:
                    try:
                        await github_client.close_pull_request(
                            github_repository.owner,
                            github_repository.repo,
                            pull_number=pull_request_number,
                        )
                    except GithubClientError as error:
                        raise CliError(t"Could not close PR #{pull_request_number}.") from error

            await _apply_orphaned_comment_cleanup(
                dry_run=dry_run,
                github_client=github_client,
                github_repository=github_repository,
                pull_request_number=pull_request_number,
                recorder=recorder,
                resolved_comments=resolved_comments,
            )
            if cleanup_bookmark:
                apply_bookmark_cleanup(
                    bookmark=bookmark,
                    cleanup_plan=cleanup_plan,
                    commit_id=last_target,
                    dry_run=dry_run,
                    jj_client=jj_client,
                    record_action=recorder.record,
                    remote_name=remote.name,
                )

        recorder.record(
            CloseAction(
                kind="saved data",
                body=t"prune orphan record for {label}",
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            next_changes = dict(state.changes)
            next_changes.pop(change_id, None)
            final_state = state.model_copy(update={"changes": next_changes})
            state_store.save(final_state)

        completed = True
        return _render_orphan_close_actions(
            actions=recorder.as_tuple(),
            blocked=recorder.blocked,
            dry_run=dry_run,
        )
    finally:
        if completed and operation_state.journal is not None:
            operation_state.journal.append(
                "completed",
                {"ordered_change_ids": (change_id,)},
            )
            _retire_superseded_orphan_close_operations(
                current_change_ids=(change_id,),
                stale_operations=operation_state.stale_close_operations,
            )
            if not recorder.blocked and final_state is not None:
                retire_submit_intents_cleared_by_cleanup(
                    current_state=final_state,
                    jj_client=jj_client,
                    stale_submit_intents=operation_state.stale_submit_intents,
                )


def _orphan_should_cleanup_bookmark(
    *,
    bookmark: str,
    cached_change: CachedChange,
    cleanup_user_bookmarks: bool,
    prefix: str,
) -> bool:
    if cached_change.manages_bookmark:
        return is_review_bookmark(bookmark, prefix=prefix)
    return cleanup_user_bookmarks


def _retire_superseded_orphan_close_operations(
    *,
    current_change_ids: tuple[str, ...],
    stale_operations: list[LoadedOperationRecord],
) -> None:
    """Mark interrupted close journals terminal after orphan cleanup covers them."""

    current_id_set = set(current_change_ids)
    for loaded in stale_operations:
        operation = loaded.operation
        if not isinstance(operation, CloseOperationRecord):
            continue
        if (
            close_intent_mode_relation(
                recorded_cleanup=operation.cleanup,
                current_cleanup=True,
            )
            == "incompatible"
        ):
            continue
        if set(operation.ordered_change_ids).issubset(current_id_set):
            append_abandoned_event(loaded.path, reason="superseded_by_close")


def _render_orphan_close_actions(
    *,
    actions: tuple[CloseAction, ...],
    blocked: bool,
    dry_run: bool,
) -> int:
    header = (
        "Close blocked:"
        if blocked
        else ("Applied close actions:" if not dry_run else "Planned close actions:")
    )
    console.output(header)
    for action in actions:
        prefix, prefix_style, body_style = close_action_presentation(action.status)
        console.output(
            ui.prefixed_line(
                f"{prefix} ",
                render_close_action_message(action),
                prefix_labels=prefix_style,
                message_labels=body_style,
            )
        )
    return 1 if blocked else 0


def _retire_blocked_orphan_close_tracking(
    *,
    cached_change: CachedChange,
    change_id: str,
    dry_run: bool,
    inspection: _OrphanedPullRequestInspection | None,
    recorder: _OrphanActionRecorder,
    revision_label: Message,
    state: ReviewState,
    state_store: ReviewStateStore,
) -> None:
    if inspection is None or inspection.state != "closed":
        return

    updated_change = _retire_cached_change(
        cached_change,
        pr_state=inspection.pull_request.state,
    )
    if updated_change == cached_change:
        return

    recorder.record(
        CloseAction(
            kind="tracking",
            body=t"mark {revision_label} as already {inspection.pull_request.state} on GitHub",
            status="planned" if dry_run else "applied",
        )
    )
    if not dry_run:
        next_changes = dict(state.changes)
        next_changes[change_id] = updated_change
        state_store.save(state.model_copy(update={"changes": next_changes}))


def _preflight_orphan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    cleanup_user_bookmarks: bool,
    dry_run: bool,
    prefix: str,
    recorder: _OrphanActionRecorder,
    remote_name: str,
    saved_commit_id: str | None,
) -> _OrphanBookmarkCleanupPlan:
    remote_state = bookmark_state.remote_target(remote_name)
    if remote_state is None or not remote_state.targets:
        branch_label = f"{bookmark}@{remote_name}"
        recorder.record(
            CloseAction(
                kind="remote branch",
                body=t"{ui.bookmark(branch_label)} already absent",
                status="planned" if dry_run else "applied",
            )
        )
    if saved_commit_id is None:
        if bookmark_state.local_target is not None or bookmark_state.local_targets or (
            remote_state is not None and remote_state.targets
        ):
            recorder.record(
                CloseAction(
                    kind="close",
                    body=(
                        t"cannot clean up saved bookmark {ui.bookmark(bookmark)} "
                        t"without a saved submitted target"
                    ),
                    status="blocked",
                )
            )
        return _OrphanBookmarkCleanupPlan(local_forget=False, remote_delete=False)
    return _plan_orphan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        cached_change=cached_change,
        cleanup_user_bookmarks=cleanup_user_bookmarks,
        commit_id=saved_commit_id,
        prefix=prefix,
        recorder=recorder,
        remote_name=remote_name,
    )


def _plan_orphan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    cleanup_user_bookmarks: bool,
    commit_id: str,
    prefix: str,
    recorder: _OrphanActionRecorder,
    remote_name: str,
) -> _OrphanBookmarkCleanupPlan:
    return plan_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        cached_change=cached_change,
        cleanup_user_bookmarks=cleanup_user_bookmarks,
        commit_id=commit_id,
        prefix=prefix,
        record_action=recorder.record,
        remote_name=remote_name,
    )


async def _preflight_orphaned_comment_cleanup(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
    recorder: _OrphanActionRecorder,
) -> tuple[_ResolvedOrphanedComment, ...]:
    resolved_comments: list[_ResolvedOrphanedComment] = []
    for kind, cached_comment_id in (
        ("navigation", cached_change.navigation_comment_id),
        ("overview", cached_change.overview_comment_id),
    ):
        comment, comment_error = await find_managed_comment(
            cached_comment_id=cached_comment_id,
            github_client=github_client,
            github_repository=github_repository,
            kind=kind,
            pull_request_number=pull_request_number,
        )
        if comment_error is not None:
            recorder.record(comment_error)
            return ()
        if comment is not None:
            resolved_comments.append(_ResolvedOrphanedComment(comment=comment, kind=kind))
    return tuple(resolved_comments)


async def _apply_orphaned_comment_cleanup(
    *,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
    recorder: _OrphanActionRecorder,
    resolved_comments: tuple[_ResolvedOrphanedComment, ...],
) -> None:
    for resolved in resolved_comments:
        recorder.record(
            CloseAction(
                kind=stack_comment_label(resolved.kind),
                body=(
                    f"delete {stack_comment_label(resolved.kind)} #{resolved.comment.id} from "
                    f"PR #{pull_request_number}"
                ),
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            try:
                await github_client.delete_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=resolved.comment.id,
                )
            except GithubClientError as error:
                if error.status_code != 404:
                    raise CliError(
                        t"Could not delete {stack_comment_label(resolved.kind)} "
                        t"#{resolved.comment.id}."
                    ) from error


async def _lookup_orphaned_pull_request(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
) -> tuple[_OrphanedPullRequestInspection | None, CloseAction | None]:
    """Verify the saved PR identity and look for live duplicate branch claims."""

    bookmark = cached_change.bookmark
    if bookmark is None:
        return (
            None,
            CloseAction(
                kind="close",
                body="cannot inspect orphaned pull request without a saved bookmark identity",
                status="blocked",
            ),
        )

    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return (
                None,
                CloseAction(
                    kind="close",
                    body=t"PR #{pull_request_number} is no longer on GitHub",
                    status="blocked",
                ),
            )
        return None, _blocked_orphaned_close_github_action()
    inspection = _inspect_orphaned_pull_request_state(pull_request)
    if pull_request.head.ref != bookmark:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because it no longer "
                    t"has saved bookmark {ui.bookmark(bookmark)} as its head ref"
                ),
                status="blocked",
            ),
        )
    expected_head_label = f"{github_repository.owner}:{bookmark}"
    if pull_request.head.label != expected_head_label:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because its head "
                    t"is {pull_request.head.label or '<unknown>'}, not "
                    t"{expected_head_label}"
                ),
                status="blocked",
            ),
        )

    try:
        branch_matches = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=(bookmark,),
        )
    except GithubClientError:
        return None, _blocked_orphaned_close_github_action()

    other_live_matches = tuple(
        candidate
        for candidate in branch_matches.get(bookmark, ())
        if candidate.number != pull_request_number
    )
    if other_live_matches:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because saved bookmark "
                    t"{ui.bookmark(bookmark)} now has multiple pull requests"
                ),
                status="blocked",
            ),
        )
    return inspection, None


def _inspect_orphaned_pull_request_state(
    pull_request: GithubPullRequest,
) -> _OrphanedPullRequestInspection:
    if pull_request.state != "closed" or pull_request.merged_at is None:
        normalized_pull_request = pull_request
    else:
        normalized_pull_request = pull_request.model_copy(update={"state": "merged"})
    state: OrphanedPullRequestState = (
        "open" if normalized_pull_request.state == "open" else "closed"
    )
    return _OrphanedPullRequestInspection(
        pull_request=normalized_pull_request,
        state=state,
    )


def _blocked_orphaned_close_github_action() -> CloseAction:
    return CloseAction(
        kind="close",
        body=(
            "cannot close pull requests tracked by jj-review without live GitHub state; "
            "fix GitHub access and retry"
        ),
        status="blocked",
    )


def _start_orphan_close_operation(
    *,
    cached_change: CachedChange,
    change_id: str,
    dry_run: bool,
    operation_lock: OperationLock,
    pull_request_number: int,
    report_stale_close_operations: ReportStaleCloseOperations,
    state_store: ReviewStateStore,
) -> _OrphanCloseOperationState:
    """Write close operation journal metadata for resumable orphan cleanup runs."""

    if dry_run:
        return _OrphanCloseOperationState(
            journal=None,
            stale_close_operations=[],
            stale_submit_intents=[],
        )

    state_dir = state_store.require_writable()
    ordered_change_ids = (change_id,)
    ordered_commit_ids = (
        (cached_change.last_submitted_commit_id,)
        if cached_change.last_submitted_commit_id is not None
        else ()
    )
    stale_close_operations = [
        loaded
        for loaded in state_store.list_operations()
        if isinstance(loaded.operation, CloseOperationRecord)
    ]
    report_stale_close_operations(
        current_change_ids=ordered_change_ids,
        current_commit_ids=ordered_commit_ids,
        current_cleanup=True,
        stale_intents=stale_close_operations,
    )
    stale_submit_intents = [
        loaded
        for loaded in state_store.list_intents()
        if isinstance(loaded.intent, SubmitIntent) and not pid_is_alive(loaded.intent.pid)
    ]
    journal = OperationJournal.begin(
        state_dir,
        operation="close",
        lock_holder=read_operation_lock_holder(state_dir),
        options={
            "cleanup": True,
            "pull_request_number": pull_request_number,
        },
        resolved_scope={
            "ordered_change_ids": ordered_change_ids,
            "ordered_commit_ids": ordered_commit_ids,
            "selected_revset": f"--pull-request {pull_request_number}",
        },
    )
    operation_lock.record_journal_path(journal.path)
    return _OrphanCloseOperationState(
        journal=journal,
        stale_close_operations=stale_close_operations,
        stale_submit_intents=stale_submit_intents,
    )
