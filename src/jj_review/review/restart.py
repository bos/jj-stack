"""Shared planning for starting fresh review tracking."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import jj_review.ui as ui
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.formatting import short_change_id
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.bookmarks import generate_bookmark_name


@dataclass(frozen=True, slots=True)
class RestartedChange:
    """One local change whose saved review identity was reset."""

    change_id: str
    new_bookmark: str
    old_bookmark: str | None
    old_pr_number: int | None
    subject: str


@dataclass(frozen=True, slots=True)
class RestartStateResult:
    """Tracking state prepared for fresh pull requests."""

    changed: tuple[RestartedChange, ...]
    state: ReviewState


def restart_state_for_stack(
    *,
    bookmark_states: dict[str, BookmarkState],
    config: RepoConfig,
    reserved_bookmarks: Iterable[str] = (),
    stack: LocalStack,
    state: ReviewState,
) -> RestartStateResult:
    """Return tracking state where selected submitted changes will use fresh PRs."""

    _ensure_stack_has_no_unlinked_changes(stack=stack, state=state)
    used_bookmarks = {
        bookmark
        for bookmark in (
            *(cached.bookmark for cached in state.changes.values()),
            *bookmark_states,
            *reserved_bookmarks,
        )
        if bookmark is not None
    }

    changes = dict(state.changes)
    restarted: list[RestartedChange] = []
    for revision in stack.revisions:
        cached_change = state.changes.get(revision.change_id)
        if cached_change is None or not cached_change_needs_restart(cached_change):
            continue
        new_bookmark = fresh_bookmark_name(
            config=config,
            revision=revision,
            old_bookmark=cached_change.bookmark,
            old_pr_number=cached_change.pr_number,
            used_bookmarks=used_bookmarks,
        )
        used_bookmarks.add(new_bookmark)
        changes[revision.change_id] = restart_cached_change(
            cached_change,
            new_bookmark=new_bookmark,
        )
        restarted.append(
            RestartedChange(
                change_id=revision.change_id,
                new_bookmark=new_bookmark,
                old_bookmark=cached_change.bookmark,
                old_pr_number=cached_change.pr_number,
                subject=revision.subject,
            )
        )

    return RestartStateResult(
        changed=tuple(restarted),
        state=state.model_copy(update={"changes": changes}) if restarted else state,
    )


def cached_change_needs_restart(cached_change: CachedChange) -> bool:
    return any(
        value is not None
        for value in (
            cached_change.last_submitted_commit_id,
            cached_change.last_submitted_parent_change_id,
            cached_change.last_submitted_stack_head_change_id,
            cached_change.pr_is_draft,
            cached_change.pr_number,
            cached_change.pr_review_decision,
            cached_change.pr_state,
            cached_change.pr_url,
            cached_change.navigation_comment_id,
            cached_change.overview_comment_id,
        )
    )


def restart_cached_change(cached_change: CachedChange, *, new_bookmark: str) -> CachedChange:
    return cached_change.model_copy(
        update={
            "bookmark": new_bookmark,
            "bookmark_ownership": "managed",
            "last_submitted_commit_id": None,
            "last_submitted_parent_change_id": None,
            "last_submitted_stack_head_change_id": None,
            "link_state": "active",
        }
    ).with_cleared_pr_identity().with_cleared_comments()


def fresh_bookmark_name(
    *,
    config: RepoConfig,
    old_bookmark: str | None,
    old_pr_number: int | None,
    revision: LocalRevision,
    used_bookmarks: set[str],
) -> str:
    base = generate_bookmark_name(revision, prefix=config.bookmark_prefix)
    short_id = short_change_id(revision.change_id)
    suffix = f"-{short_id}"
    stem = base[: -len(suffix)] if base.endswith(suffix) else base
    markers = _fresh_markers(old_pr_number=old_pr_number)
    for marker in markers:
        candidate = f"{stem}-{marker}-{short_id}"
        if candidate == old_bookmark or candidate in used_bookmarks:
            continue
        return candidate
    raise CliError(
        t"Could not choose a fresh review bookmark for "
        t"{ui.change_id(revision.change_id)}."
    )


def _fresh_markers(*, old_pr_number: int | None) -> Iterable[str]:
    if old_pr_number is not None:
        yield f"fresh-pr{old_pr_number}"
    yield "fresh"
    for attempt in range(2, 100):
        yield f"fresh-{attempt}"


def _ensure_stack_has_no_unlinked_changes(
    *,
    stack: LocalStack,
    state: ReviewState,
) -> None:
    unlinked = tuple(
        revision
        for revision in stack.revisions
        if (cached := state.changes.get(revision.change_id)) is not None
        and cached.is_unlinked
    )
    if not unlinked:
        return
    if len(unlinked) == 1:
        revision = unlinked[0]
        raise CliError(
            t"Change {ui.change_id(revision.change_id)} is unlinked from review tracking.",
            hint=t"Use {ui.cmd('relink')} if it should be attached to review again.",
        )
    raise CliError(
        t"Selected stack contains unlinked changes: "
        t"{ui.join(lambda revision: ui.change_id(revision.change_id), unlinked)}.",
        hint=t"Use {ui.cmd('relink')} for changes that should be attached to review again.",
    )
