"""Staleness detection for cleanup: tracked changes and orphaned local review bookmarks.

Both passes share one definition of "stale": the target no longer resolves to exactly one
reviewable revision that still participates in a supported stack.
"""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.bookmarks import is_review_bookmark
from jj_stack.review.discovery import discover_stacks_from_revisions

from .shared import CleanupAction, OrphanLocalBookmarkCleanupPlan


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
    supported_commit_ids = _supported_review_commit_ids_for_revisions(
        context=context,
        revisions=candidate_revisions,
    )
    for revision in candidate_revisions:
        if revision.commit_id not in supported_commit_ids:
            reasons[revision.change_id] = (
                "local change no longer participates in a supported stack"
            )
    return reasons


def _supported_review_commit_ids_for_revisions(
    *,
    context: CommandContext,
    revisions: tuple[LocalRevision, ...],
) -> set[str]:
    stacks = discover_stacks_from_revisions(
        jj_client=context.jj_client,
        revisions=revisions,
    )
    return {
        revision.commit_id for stack in stacks for revision in stack.revisions
    }


def _plan_orphan_local_bookmark_cleanups(
    *,
    bookmark_states: dict[str, BookmarkState],
    context: CommandContext,
    tracked_bookmarks: set[str],
) -> tuple[OrphanLocalBookmarkCleanupPlan, ...]:
    prefix = context.config.bookmark_prefix
    candidate_bookmark_states: list[BookmarkState] = []
    plans: list[OrphanLocalBookmarkCleanupPlan] = []
    for bookmark, bookmark_state in sorted(bookmark_states.items()):
        if bookmark in tracked_bookmarks or not is_review_bookmark(bookmark, prefix=prefix):
            continue
        if not bookmark_state.local_targets:
            continue
        if len(bookmark_state.local_targets) > 1:
            plans.append(
                OrphanLocalBookmarkCleanupPlan(
                    bookmark=bookmark,
                    action=CleanupAction(
                        kind="local bookmark",
                        status="blocked",
                        body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
                    ),
                )
            )
            continue
        if bookmark_state.local_target is not None:
            candidate_bookmark_states.append(bookmark_state)

    target_commit_ids = tuple(
        bookmark_state.local_target
        for bookmark_state in candidate_bookmark_states
        if bookmark_state.local_target is not None
    )
    if not target_commit_ids:
        return tuple(plans)

    revisions_by_commit_id = {
        revision.commit_id: revision
        for revision in context.jj_client.query_revisions_by_commit_ids(target_commit_ids)
    }
    reviewable_revisions = tuple(
        revision
        for bookmark_state in candidate_bookmark_states
        for revision in (revisions_by_commit_id.get(bookmark_state.local_target or ""),)
        if revision is not None and revision.is_reviewable()
    )
    supported_commit_ids = _supported_review_commit_ids_for_revisions(
        context=context,
        revisions=reviewable_revisions,
    )

    for bookmark_state in candidate_bookmark_states:
        orphan_plan = _plan_orphan_local_bookmark_cleanup(
            bookmark_state=bookmark_state,
            revision=revisions_by_commit_id.get(bookmark_state.local_target or ""),
            supported_commit_ids=supported_commit_ids,
        )
        if orphan_plan is not None:
            plans.append(orphan_plan)
    return tuple(plans)


def _plan_orphan_local_bookmark_cleanup(
    *,
    bookmark_state: BookmarkState,
    revision: LocalRevision | None,
    supported_commit_ids: set[str],
) -> OrphanLocalBookmarkCleanupPlan | None:
    bookmark = bookmark_state.name
    local_target = bookmark_state.local_target
    if local_target is None:
        return None

    if revision is None:
        stale_reason = "target is no longer visible locally"
    else:
        if not revision.is_reviewable():
            stale_reason = "target is no longer reviewable"
        elif revision.commit_id not in supported_commit_ids:
            stale_reason = "target no longer participates in a supported stack"
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
