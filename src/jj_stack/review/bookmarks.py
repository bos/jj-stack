"""Bookmark naming, rediscovery, and resolution helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import (
    generate_review_branch,
    review_branch_matches_change,
)
from jj_stack.review.change_status import classify_review_change_without_pull_request
from jj_stack.ui import Message

BookmarkSource = Literal["saved", "discovered", "generated"]


@dataclass(frozen=True, slots=True)
class ResolvedBookmark:
    """Resolved bookmark for one local revision."""

    bookmark: str
    change_id: str
    source: BookmarkSource


class RevisionWithChangeId(Protocol):
    """Minimal revision shape needed for bookmark discovery."""

    @property
    def change_id(self) -> str: ...

    @property
    def commit_id(self) -> str: ...


class BookmarkResolver:
    """Resolve bookmark names without changing tracking state."""

    def __init__(
        self,
        review_identities: Mapping[str, ReviewIdentity],
        *,
        discovered_bookmarks: Mapping[str, str] | None = None,
    ) -> None:
        self._review_identities = review_identities
        self._discovered_bookmarks = discovered_bookmarks or {}

    def resolve_revisions(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> tuple[ResolvedBookmark, ...]:
        """Resolve bookmarks from identity, explicit matches, discovery, or naming."""

        resolutions: list[ResolvedBookmark] = []
        for revision in revisions:
            review_identity = self._review_identities.get(revision.change_id)
            if review_identity is not None:
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=review_identity.head_ref,
                        change_id=revision.change_id,
                        source="saved",
                    )
                )
                continue
            if discovered_bookmark := self._discovered_bookmarks.get(revision.change_id):
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=discovered_bookmark,
                        change_id=revision.change_id,
                        source="discovered",
                    )
                )
                continue

            bookmark = generate_review_branch(revision)
            resolutions.append(
                ResolvedBookmark(
                    bookmark=bookmark,
                    change_id=revision.change_id,
                    source="generated",
                )
            )
        return tuple(resolutions)


LocalBookmarkForgetSafety = Literal["absent", "conflicted", "diverged", "unverified", "safe"]


def bookmark_cleanup_allowed(
    *,
    bookmark: str,
    change_id: str,
) -> bool:
    """Whether cleanup may touch this managed review bookmark."""

    return review_branch_matches_change(bookmark, change_id)


def classify_local_bookmark_forget(
    *,
    bookmark_state: BookmarkState,
    expected_commit_id: str | None,
) -> LocalBookmarkForgetSafety:
    """Classify whether forgetting one local bookmark is provably safe."""

    if not bookmark_state.local_targets:
        return "absent"
    if len(bookmark_state.local_targets) > 1:
        return "conflicted"
    if expected_commit_id is None:
        return "unverified"
    if bookmark_state.local_target != expected_commit_id:
        return "diverged"
    return "safe"


def local_bookmark_forget_blocked_body(
    bookmark: str,
    safety: Literal["conflicted", "diverged"],
) -> Message:
    """Return the standard action body for a blocked local bookmark forget."""

    if safety == "conflicted":
        return t"cannot forget {ui.bookmark(bookmark)} because it is conflicted"
    return (
        t"cannot forget {ui.bookmark(bookmark)} because it already points to a different revision"
    )


def discover_bookmarks_for_revisions(
    *,
    bookmark_states: dict[str, BookmarkState],
    remote_name: str,
    revisions: tuple[RevisionWithChangeId, ...],
) -> dict[str, str]:
    discovered: dict[str, str] = {}
    for revision in revisions:
        candidates = [
            bookmark
            for bookmark, bookmark_state in bookmark_states.items()
            if review_branch_matches_change(bookmark, revision.change_id)
            and _bookmark_state_is_discoverable(bookmark_state, remote_name)
        ]
        if not candidates:
            continue
        unique_candidates = sorted(set(candidates))
        if len(unique_candidates) > 1:
            raise CliError(
                t"Could not safely rediscover the bookmark for change "
                t"{ui.change_id(revision.change_id)}: multiple existing bookmarks match "
                t"its stable change-ID suffix: {ui.join(ui.bookmark, unique_candidates)}."
            )
        discovered[revision.change_id] = unique_candidates[0]
    return discovered


def ensure_unique_bookmarks(resolutions: tuple[ResolvedBookmark, ...]) -> None:
    bookmarks_to_changes: dict[str, list[str]] = {}
    for resolution in resolutions:
        bookmarks_to_changes.setdefault(resolution.bookmark, []).append(resolution.change_id)

    duplicates = {
        bookmark: change_ids
        for bookmark, change_ids in bookmarks_to_changes.items()
        if len(change_ids) > 1
    }
    if not duplicates:
        return

    collisions = ui.join(
        lambda item: t"{ui.bookmark(item[0])} for changes {ui.join(ui.change_id, item[1])}",
        sorted(duplicates.items()),
    )
    raise CliError(
        t"Selected stack resolves multiple changes to the same bookmark: {collisions}.",
        hint="Change an untracked change's subject or repair the saved review links.",
    )


def find_changes_by_bookmark(
    review_identities: Mapping[str, ReviewIdentity],
    bookmark: str,
) -> tuple[str, ...]:
    """Return change_ids of any saved record whose bookmark matches.

    Used to detect cross-claim collisions before mutating remote state — for
    example, when `unstack --cleanup --pull-request <pr>` is asked to delete an
    orphaned PR's branch but the same bookmark is now claimed by another live
    review record.
    """

    return tuple(
        change_id
        for change_id, review_identity in review_identities.items()
        if review_identity.head_ref == bookmark
    )


def _bookmark_state_is_discoverable(bookmark_state: BookmarkState, remote_name: str) -> bool:
    if bookmark_state.local_targets:
        return True
    remote_state = bookmark_state.remote_target(remote_name)
    remote_status = classify_review_change_without_pull_request(
        commit_id=None,
        remote_state=remote_state,
    )
    return remote_status.remote_branch != "absent"
