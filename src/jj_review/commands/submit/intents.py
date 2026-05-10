"""Submit intent records and the bookmark repairs they imply for stale runs."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from jj_review import console
from jj_review.formatting import short_change_id
from jj_review.github.resolution import parse_github_repo
from jj_review.models.bookmarks import GitRemote
from jj_review.models.stack import LocalStack
from jj_review.review.bookmarks import BookmarkResolutionResult
from jj_review.review.intents import describe_intent
from jj_review.review.submit_recovery import (
    SubmitRecoveryIdentity,
    SubmitStatusDecision,
    submit_status_decision,
)
from jj_review.state.journal import (
    LoadedOperationRecord,
    OperationJournal,
    SubmitOperationRecord,
    scan_incomplete_operation_records,
)
from jj_review.state.operation_lock import OperationLock, read_operation_lock_holder
from jj_review.state.store import ReviewStateStore
from jj_review.system import pid_is_alive

from .models import InterruptedRemoteBookmarkRepairer, SubmitOperationState


def start_submit_intent(
    *,
    bookmark_result: BookmarkResolutionResult,
    dry_run: bool,
    github_repository,
    operation_lock: OperationLock,
    remote_name: str,
    stack: LocalStack,
    state_store: ReviewStateStore,
) -> SubmitOperationState:
    """Prepare submit operation state before any remote mutation begins."""

    ordered_change_ids = tuple(revision.change_id for revision in stack.revisions)
    ordered_commit_ids = tuple(revision.commit_id for revision in stack.revisions)
    operation = SubmitOperationRecord(
        kind="submit",
        path=Path(),
        pid=os.getpid(),
        label=(
            f"submit for {short_change_id(stack.head.change_id)} (from {stack.selected_revset})"
        ),
        display_revset=stack.selected_revset,
        ordered_commit_ids=ordered_commit_ids,
        remote_name=remote_name,
        github_host=github_repository.host,
        github_owner=github_repository.owner,
        github_repo=github_repository.repo,
        ordered_change_ids=ordered_change_ids,
        bookmarks={
            revision.change_id: resolution.bookmark
            for revision, resolution in zip(
                stack.revisions,
                bookmark_result.resolutions,
                strict=True,
            )
        },
        started_at=datetime.now(UTC).isoformat(),
    )
    if dry_run:
        stale_operations = _list_stale_submit_operations_without_waiting(
            state_store=state_store,
            operation=operation,
        )
        _report_stale_submit_operations(
            current_operation=operation,
            ordered_change_ids=ordered_change_ids,
            ordered_commit_ids=ordered_commit_ids,
            stale_operations=stale_operations,
        )
        return SubmitOperationState(
            journal=None,
            operation=operation,
            stale_operations=stale_operations,
        )

    state_dir = state_store.require_writable()
    stale_operations = [
        loaded
        for loaded in state_store.list_operations()
        if isinstance(loaded.operation, SubmitOperationRecord)
    ]
    _report_stale_submit_operations(
        current_operation=operation,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        stale_operations=stale_operations,
    )
    journal = OperationJournal.begin(
        state_dir,
        operation="submit",
        lock_holder=read_operation_lock_holder(state_dir),
        options={
            "remote_name": remote_name,
            "github_host": github_repository.host,
            "github_owner": github_repository.owner,
            "github_repo": github_repository.repo,
        },
        resolved_scope={
            "bookmarks": operation.bookmarks,
            "ordered_change_ids": ordered_change_ids,
            "ordered_commit_ids": ordered_commit_ids,
            "selected_revset": stack.selected_revset,
        },
    )
    operation_lock.record_journal_path(journal.path)
    operation = replace(operation, path=journal.path)
    return SubmitOperationState(
        journal=journal,
        operation=operation,
        stale_operations=stale_operations,
    )


def _report_stale_submit_operations(
    *,
    current_operation: SubmitOperationRecord,
    ordered_change_ids: tuple[str, ...],
    ordered_commit_ids: tuple[str, ...],
    stale_operations: list[LoadedOperationRecord],
) -> None:
    """Render resumable submit operation diagnostics for the operator."""

    for loaded in stale_operations:
        if not isinstance(loaded.operation, SubmitOperationRecord):
            continue
        operation = loaded.operation
        decision = submit_status_decision(
            intent=operation,
            current_change_ids=ordered_change_ids,
            current_commit_ids=ordered_commit_ids,
            current_identity=SubmitRecoveryIdentity.from_operation(current_operation),
        )
        description = describe_intent(operation)
        if decision is SubmitStatusDecision.CONTINUE:
            console.note(t"Continuing interrupted {description}", soft_wrap=True)
        elif decision is SubmitStatusDecision.CURRENT_STACK:
            console.note(
                t"Note: interrupted {description} does not match the current stack "
                t"exactly. This submit will use the current stack.",
                soft_wrap=True,
            )
        elif decision is SubmitStatusDecision.INSPECT:
            console.note(
                t"Note: interrupted {description} matches the current stack, "
                t"but its recorded submit target does not. This submit will use "
                t"the current stack.",
                soft_wrap=True,
            )
        else:
            console.note(
                t"Note: incomplete operation outstanding: {description}",
                soft_wrap=True,
            )


def _list_stale_submit_operations_without_waiting(
    *,
    state_store: ReviewStateStore,
    operation: SubmitOperationRecord,
) -> list[LoadedOperationRecord]:
    return [
        loaded
        for loaded in state_store.list_operations()
        if loaded.operation.kind == operation.kind and not pid_is_alive(loaded.operation.pid)
    ]


def repair_interrupted_untracked_remote_bookmarks(
    *,
    client: InterruptedRemoteBookmarkRepairer,
    remote: GitRemote,
    state_dir: Path,
) -> None:
    current_github_repository = parse_github_repo(remote)
    if current_github_repository is None:
        return

    stale_submit_operations: list[SubmitOperationRecord] = []
    for loaded in scan_incomplete_operation_records(state_dir):
        intent = loaded.operation
        if not isinstance(intent, SubmitOperationRecord):
            continue
        if pid_is_alive(intent.pid):
            continue
        if intent.remote_name != remote.name:
            continue
        if (
            intent.github_host,
            intent.github_owner,
            intent.github_repo,
        ) != (
            current_github_repository.host,
            current_github_repository.owner,
            current_github_repository.repo,
        ):
            continue
        stale_submit_operations.append(intent)

    if not stale_submit_operations:
        return

    bookmarks = tuple(
        sorted(
            {
                bookmark
                for loaded in stale_submit_operations
                for bookmark in loaded.bookmarks.values()
            }
        )
    )
    if not bookmarks:
        return

    client.fetch_remote(remote=remote.name)
    bookmark_states = client.list_bookmark_states(bookmarks)
    for bookmark in bookmarks:
        bookmark_state = bookmark_states.get(bookmark)
        if bookmark_state is None:
            continue
        remote_state = bookmark_state.remote_target(remote.name)
        if remote_state is None or remote_state.is_tracked:
            continue
        local_target = bookmark_state.local_target
        if local_target is None or remote_state.target != local_target:
            continue
        client.track_bookmark(remote=remote.name, bookmark=bookmark)
