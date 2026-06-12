"""Bookmark naming, rediscovery, and resolution helpers."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import jj_stack.ui as ui
from jj_stack.config import DEFAULT_BOOKMARK_PREFIX
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.review_state import BookmarkOwnership, CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.change_status import classify_review_change_without_pull_request
from jj_stack.ui import Message

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_SLUG = "change"

BookmarkSource = Literal["saved", "matched", "discovered", "generated"]


@dataclass(frozen=True, slots=True)
class ResolvedBookmark:
    """Resolved bookmark for one local revision."""

    bookmark: str
    change_id: str
    source: BookmarkSource


@dataclass(frozen=True, slots=True)
class BookmarkResolutionResult:
    """Bookmark resolutions plus the updated tracking data."""

    changed: bool
    resolutions: tuple[ResolvedBookmark, ...]
    state: ReviewState


class RevisionWithChangeId(Protocol):
    """Minimal revision shape needed for bookmark discovery."""

    @property
    def change_id(self) -> str: ...

    @property
    def commit_id(self) -> str: ...


class BookmarkResolver:
    """Resolve bookmark names using saved-data-first semantics."""

    def __init__(
        self,
        state: ReviewState,
        *,
        prefix: str = DEFAULT_BOOKMARK_PREFIX,
        matched_bookmarks: Mapping[str, str] | None = None,
        discovered_bookmarks: Mapping[str, str] | None = None,
    ) -> None:
        self._state = state
        self._prefix = prefix
        self._matched_bookmarks = matched_bookmarks or {}
        self._discovered_bookmarks = discovered_bookmarks or {}

    def pin_revisions(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> BookmarkResolutionResult:
        """Resolve bookmarks and pin generated names into the returned state."""

        changed = False
        changes = dict(self._state.changes)
        resolutions: list[ResolvedBookmark] = []
        for revision in revisions:
            cached_change = changes.get(revision.change_id)
            if cached_change and cached_change.bookmark:
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=cached_change.bookmark,
                        change_id=revision.change_id,
                        source="saved",
                    )
                )
                continue
            if matched_bookmark := self._matched_bookmarks.get(revision.change_id):
                updated_change = _updated_cached_change(
                    cached_change,
                    matched_bookmark,
                    bookmark_ownership=bookmark_ownership_for_source("matched"),
                )
                if updated_change != cached_change:
                    changes[revision.change_id] = updated_change
                    changed = True
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=matched_bookmark,
                        change_id=revision.change_id,
                        source="matched",
                    )
                )
                continue
            if discovered_bookmark := self._discovered_bookmarks.get(revision.change_id):
                changes[revision.change_id] = _updated_cached_change(
                    cached_change,
                    discovered_bookmark,
                )
                resolutions.append(
                    ResolvedBookmark(
                        bookmark=discovered_bookmark,
                        change_id=revision.change_id,
                        source="discovered",
                    )
                )
                changed = True
                continue

            bookmark = generate_bookmark_name(
                revision,
                prefix=self._prefix,
            )
            changes[revision.change_id] = _updated_cached_change(cached_change, bookmark)
            resolutions.append(
                ResolvedBookmark(
                    bookmark=bookmark,
                    change_id=revision.change_id,
                    source="generated",
                )
            )
            changed = True

        return BookmarkResolutionResult(
            changed=changed,
            resolutions=tuple(resolutions),
            state=self._state.model_copy(update={"changes": changes}),
        )


def bookmark_glob(prefix: str) -> str:
    """Return the wildcard pattern for managed review branches."""

    return f"{prefix}/*"


def is_review_bookmark(bookmark: str, *, prefix: str) -> bool:
    """Whether `bookmark` uses the configured managed review prefix."""

    return bookmark.startswith(f"{prefix}/")


LocalBookmarkForgetSafety = Literal["absent", "conflicted", "diverged", "unverified", "safe"]


def bookmark_cleanup_allowed(
    *,
    bookmark: str,
    bookmark_managed: bool,
    cleanup_user_bookmarks: bool,
    prefix: str,
) -> bool:
    """Whether cleanup may touch this bookmark at all.

    Managed bookmarks are cleanable only under the configured review prefix; external
    bookmarks (e.g. matched through `use_bookmarks`) need the explicit
    `cleanup_user_bookmarks` opt-in.
    """

    if bookmark_managed:
        return is_review_bookmark(bookmark, prefix=prefix)
    return cleanup_user_bookmarks


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
        t"cannot forget {ui.bookmark(bookmark)} because it already points "
        t"to a different revision"
    )


def generate_bookmark_name(
    revision: LocalRevision,
    *,
    prefix: str = DEFAULT_BOOKMARK_PREFIX,
) -> str:
    """Generate the default bookmark for a change."""

    first_line = revision.description.splitlines()[0] if revision.description else ""
    slug = _NON_ALNUM_RE.sub("-", first_line.lower()).strip("-") or _DEFAULT_SLUG
    return f"{prefix}/{slug}-{short_change_id(revision.change_id)}"


def match_bookmarks_for_revisions(
    *,
    bookmark_states: dict[str, BookmarkState],
    patterns: tuple[str, ...],
    revisions: tuple[RevisionWithChangeId, ...],
    remote_name: str | None,
) -> dict[str, str]:
    """Match existing bookmarks to revisions by bookmark glob and commit target."""

    if not patterns:
        return {}

    matched: dict[str, str] = {}
    for revision in revisions:
        candidates = [
            bookmark
            for bookmark, bookmark_state in bookmark_states.items()
            if any(fnmatch.fnmatchcase(bookmark, pattern) for pattern in patterns)
            and _bookmark_state_matches_revision(
                bookmark_state=bookmark_state,
                commit_id=revision.commit_id,
                remote_name=remote_name,
            )
        ]
        unique_candidates = sorted(set(candidates))
        if len(unique_candidates) > 1:
            raise CliError(
                t"Could not safely select a bookmark for change "
                t"{ui.change_id(revision.change_id)}: multiple existing bookmarks match "
                t"the configured bookmark patterns: "
                t"{ui.join(ui.bookmark, unique_candidates)}."
            )
        if unique_candidates:
            matched[revision.change_id] = unique_candidates[0]
    return matched


def discover_bookmarks_for_revisions(
    *,
    bookmark_states: dict[str, BookmarkState],
    prefix: str = DEFAULT_BOOKMARK_PREFIX,
    remote_name: str,
    revisions: tuple[RevisionWithChangeId, ...],
) -> dict[str, str]:
    discovered: dict[str, str] = {}
    for revision in revisions:
        candidates = [
            bookmark
            for bookmark, bookmark_state in bookmark_states.items()
            if bookmark_matches_generated_change_id(
                bookmark,
                revision.change_id,
                prefix=prefix,
            )
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
        hint="Use distinct bookmarks or narrower --use-bookmarks patterns before submitting.",
    )


def find_changes_by_bookmark(
    state: ReviewState,
    bookmark: str,
) -> tuple[str, ...]:
    """Return change_ids of any saved record whose bookmark matches.

    Used to detect cross-claim collisions before mutating remote state — for
    example, when `unstack --cleanup --pull-request <pr>` is asked to delete an
    orphaned PR's branch but the same bookmark is now claimed by another live
    review record (typically through a `use_bookmarks` pattern).

    All records that pin the bookmark are returned, including unlinked ones —
    they still own a name and must not be silently overwritten.
    """

    return tuple(
        change_id
        for change_id, cached_change in state.changes.items()
        if cached_change.bookmark == bookmark
    )


def bookmark_matches_generated_change_id(
    bookmark: str,
    change_id: str,
    *,
    prefix: str = DEFAULT_BOOKMARK_PREFIX,
) -> bool:
    return is_review_bookmark(
        bookmark,
        prefix=prefix,
    ) and bookmark.endswith(f"-{short_change_id(change_id)}")


def bookmark_ownership_for_source(source: BookmarkSource) -> BookmarkOwnership:
    """Return whether jj-stack should clean up a bookmark from this source."""

    return "external" if source == "matched" else "managed"


def _bookmark_state_is_discoverable(bookmark_state: BookmarkState, remote_name: str) -> bool:
    if bookmark_state.local_targets:
        return True
    remote_state = bookmark_state.remote_target(remote_name)
    remote_status = classify_review_change_without_pull_request(
        commit_id=None,
        remote_state=remote_state,
    )
    return remote_status.remote_branch != "absent"


def _bookmark_state_matches_revision(
    *,
    bookmark_state: BookmarkState,
    commit_id: str,
    remote_name: str | None,
) -> bool:
    if bookmark_state.local_target == commit_id:
        return True
    if remote_name is None:
        return False
    remote_state = bookmark_state.remote_target(remote_name)
    remote_status = classify_review_change_without_pull_request(
        commit_id=commit_id,
        remote_state=remote_state,
    )
    return remote_status.remote_branch_matches_commit is True


def _updated_cached_change(
    cached_change: CachedChange | None,
    bookmark: str,
    *,
    bookmark_ownership: BookmarkOwnership = "managed",
) -> CachedChange:
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            bookmark_ownership=bookmark_ownership,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": bookmark_ownership,
        }
    )
