"""Submit intent records and the bookmark repairs they imply for stale runs."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from jj_review import console
from jj_review.formatting import short_change_id
from jj_review.github.resolution import parse_github_repo
from jj_review.models.bookmarks import GitRemote
from jj_review.models.intent import LoadedIntent, SubmitIntent
from jj_review.models.stack import LocalStack
from jj_review.review.bookmarks import BookmarkResolutionResult
from jj_review.review.intents import describe_intent
from jj_review.review.submit_recovery import (
    SubmitRecoveryIdentity,
    SubmitStatusDecision,
    submit_status_decision,
)
from jj_review.state.intents import (
    check_same_kind_intent,
    scan_intents,
    write_new_intent,
)
from jj_review.state.store import ReviewStateStore
from jj_review.system import pid_is_alive

from .models import InterruptedRemoteBookmarkRepairer, SubmitIntentState


def start_submit_intent(
    *,
    bookmark_result: BookmarkResolutionResult,
    dry_run: bool,
    github_repository,
    remote_name: str,
    stack: LocalStack,
    state_store: ReviewStateStore,
) -> SubmitIntentState:
    """Prepare submit intent state before any remote mutation begins."""

    ordered_change_ids = tuple(revision.change_id for revision in stack.revisions)
    ordered_commit_ids = tuple(revision.commit_id for revision in stack.revisions)
    intent = SubmitIntent(
        kind="submit",
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
        stale_intents = _list_stale_submit_intents_without_waiting(
            state_store=state_store,
            intent=intent,
        )
        _report_stale_submit_intents(
            current_intent=intent,
            ordered_change_ids=ordered_change_ids,
            ordered_commit_ids=ordered_commit_ids,
            stale_intents=stale_intents,
        )
        return SubmitIntentState(intent=intent, intent_path=None, stale_intents=stale_intents)

    state_dir = state_store.require_writable()
    stale_intents = check_same_kind_intent(state_dir, intent)
    _report_stale_submit_intents(
        current_intent=intent,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        stale_intents=stale_intents,
    )
    return SubmitIntentState(
        intent=intent,
        intent_path=write_new_intent(state_dir, intent),
        stale_intents=stale_intents,
    )


def _report_stale_submit_intents(
    *,
    current_intent: SubmitIntent,
    ordered_change_ids: tuple[str, ...],
    ordered_commit_ids: tuple[str, ...],
    stale_intents: list[LoadedIntent],
) -> None:
    """Render resumable submit intent diagnostics for the operator."""

    for loaded in stale_intents:
        if not isinstance(loaded.intent, SubmitIntent):
            continue
        decision = submit_status_decision(
            intent=loaded.intent,
            current_change_ids=ordered_change_ids,
            current_commit_ids=ordered_commit_ids,
            current_identity=SubmitRecoveryIdentity.from_intent(current_intent),
        )
        description = describe_intent(loaded.intent)
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


def _list_stale_submit_intents_without_waiting(
    *,
    state_store: ReviewStateStore,
    intent: SubmitIntent,
) -> list[LoadedIntent]:
    return [
        loaded
        for loaded in state_store.list_intents()
        if loaded.intent.kind == intent.kind and not pid_is_alive(loaded.intent.pid)
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

    stale_submit_intents: list[SubmitIntent] = []
    for loaded in scan_intents(state_dir):
        intent = loaded.intent
        if not isinstance(intent, SubmitIntent):
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
        stale_submit_intents.append(intent)

    if not stale_submit_intents:
        return

    bookmarks = tuple(
        sorted(
            {
                bookmark
                for loaded in stale_submit_intents
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
