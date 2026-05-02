"""Planning helpers for the land command."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from jj_review import ui
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState
from jj_review.review.bookmarks import is_review_bookmark
from jj_review.review.status import (
    PreparedRevision,
    PreparedStatus,
    ReviewStatusRevision,
    StatusResult,
)
from jj_review.ui import Message

from .models import (
    BookmarkStateReader,
    LandAction,
    LandPlan,
    LandRevision,
    ReviewBookmarkCleanupPlan,
)

_DivergenceKind = Literal["in_sync", "diff_equivalent", "content_divergent"]


def build_land_plan(
    *,
    bypass_readiness: bool,
    client: JjClient,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
    trunk_branch: str,
) -> LandPlan:
    path_revisions = _resolve_land_path_revisions(
        prepared_status=prepared_status,
        status_result=status_result,
    )
    planned_revisions, boundary_action = _collect_landable_prefix(
        bypass_readiness=bypass_readiness,
        classify_divergence=lambda local_commit_id, remote_target: _classify_revision_divergence(
            client=client,
            local_commit_id=local_commit_id,
            remote_target=remote_target,
        ),
        path_revisions=path_revisions,
    )

    if not planned_revisions and boundary_action is None:
        boundary_action = LandAction(
            kind="boundary",
            body="No changes on the selected stack are ready to land.",
            status="blocked",
        )
    return LandPlan(
        blocked=not planned_revisions,
        boundary_action=boundary_action,
        planned_revisions=tuple(planned_revisions),
        push_trunk=bool(planned_revisions),
        trunk_branch=trunk_branch,
    )


def _classify_revision_divergence(
    *,
    client: JjClient,
    local_commit_id: str,
    remote_target: str | None,
) -> _DivergenceKind:
    """Classify how the local commit differs from the remote review branch tip."""

    if remote_target is None or remote_target == local_commit_id:
        return "in_sync"
    local_diff = client.get_commit_diff(local_commit_id)
    remote_diff = client.get_commit_diff(remote_target)
    if local_diff == remote_diff:
        return "diff_equivalent"
    return "content_divergent"

def _resolve_land_path_revisions(
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
) -> tuple[tuple[PreparedRevision, ReviewStatusRevision], ...]:
    revisions_by_change_id = {
        revision.change_id: revision for revision in status_result.revisions
    }
    path_revisions: list[tuple[PreparedRevision, ReviewStatusRevision]] = []
    for prepared_revision in prepared_status.prepared.status_revisions:
        change_id = prepared_revision.revision.change_id
        revision = revisions_by_change_id.get(change_id)
        if revision is None:
            raise AssertionError(
                f"Prepared land revision {change_id} is missing from the status result."
            )
        path_revisions.append((prepared_revision, revision))
    return tuple(path_revisions)


def _collect_landable_prefix(
    *,
    bypass_readiness: bool,
    classify_divergence: Callable[[str, str | None], _DivergenceKind],
    path_revisions: tuple[tuple[PreparedRevision, ReviewStatusRevision], ...],
) -> tuple[tuple[LandRevision, ...], LandAction | None]:
    planned_revisions: list[LandRevision] = []
    for prepared_revision, revision in path_revisions:
        boundary_message = _land_boundary_message(
            bypass_readiness=bypass_readiness,
            classify_divergence=classify_divergence,
            prepared_revision=prepared_revision,
            revision=revision,
        )
        if boundary_message is not None:
            return tuple(planned_revisions), LandAction(
                kind="boundary",
                body=boundary_message,
                status="blocked" if not planned_revisions else "planned",
            )
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is None or pull_request_lookup.pull_request is None:
            raise AssertionError("Landable revisions require resolved pull requests.")
        local_commit_id = prepared_revision.revision.commit_id
        remote_target = (
            revision.remote_state.target if revision.remote_state is not None else None
        )
        divergence = classify_divergence(local_commit_id, remote_target)
        planned_revisions.append(
            LandRevision(
                bookmark=revision.bookmark,
                bookmark_managed=(
                    revision.cached_change.manages_bookmark
                    if revision.cached_change is not None
                    else revision.bookmark_source != "matched"
                ),
                change_id=revision.change_id,
                commit_id=local_commit_id,
                needs_resubmit=divergence == "diff_equivalent",
                pull_request_number=pull_request_lookup.pull_request.number,
                subject=revision.subject,
            )
        )
    return tuple(planned_revisions), None


def _land_boundary_message(
    *,
    bypass_readiness: bool,
    classify_divergence: Callable[[str, str | None], _DivergenceKind],
    prepared_revision: PreparedRevision,
    revision: ReviewStatusRevision,
) -> Message | None:
    if prepared_revision.revision.conflict:
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"this change still has unresolved conflicts"
        )
    if revision.link_state == "unlinked":
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"this change is unlinked from review tracking; run {ui.cmd('relink')} first"
        )
    if revision.local_divergent:
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"multiple visible revisions still share that change ID"
        )
    pull_request_lookup = revision.pull_request_lookup
    if pull_request_lookup is None:
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"GitHub pull request state is unavailable"
        )
    if pull_request_lookup.state == "open":
        pull_request = pull_request_lookup.pull_request
        if pull_request is None:
            raise AssertionError("Open land boundary requires a pull request payload.")
        if pull_request_lookup.review_decision_error is not None:
            detail = pull_request_lookup.review_decision_error
            return (
                t"before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because {detail}"
            )
        remote_target = (
            revision.remote_state.target if revision.remote_state is not None else None
        )
        if (
            classify_divergence(prepared_revision.revision.commit_id, remote_target)
            == "content_divergent"
        ):
            return (
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"the local change differs from what reviewers approved; rerun "
                t"{ui.cmd('submit')} to update the PR and request re-review"
            )
        if pull_request.is_draft:
            if bypass_readiness:
                return None
            return (
                t"before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because PR #{pull_request.number} is still a draft"
            )
        if pull_request_lookup.review_decision == "changes_requested":
            if bypass_readiness:
                return None
            return (
                t"before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because PR #{pull_request.number} has changes requested"
            )
        if pull_request_lookup.review_decision != "approved":
            if bypass_readiness:
                return None
            return (
                t"before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because PR #{pull_request.number} is not approved"
            )
        return None
    if pull_request_lookup.state == "missing":
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"GitHub no longer reports a pull request for its branch; run "
            t"{ui.cmd('status --fetch')} or {ui.cmd('relink')} first"
        )
    if pull_request_lookup.state == "ambiguous":
        detail = pull_request_lookup.message or "GitHub reports an ambiguous PR link"
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"{detail} Run {ui.cmd('status --fetch')} and repair the PR link with "
            t"{ui.cmd('relink')}."
        )
    if pull_request_lookup.state == "error":
        detail = pull_request_lookup.message or "GitHub lookup failed"
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because {detail}"
        )
    pull_request = pull_request_lookup.pull_request
    if pull_request is None:
        raise AssertionError("Closed land boundary requires a pull request payload.")
    if pull_request.state == "merged":
        return (
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"PR #{pull_request.number} is already merged; run "
            t"{ui.cmd('cleanup --rebase')} first"
        )
    return (
        t"before {revision.subject} {ui.change_id(revision.change_id)} because "
        t"PR #{pull_request.number} is closed without merge"
    )


def _plan_review_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_managed: bool,
    cleanup_user_bookmarks: bool,
    prefix: str,
    bookmark_state: BookmarkState,
    change_id: str,
    commit_id: str,
) -> ReviewBookmarkCleanupPlan | None:
    """Validate whether `land` can forget one landed local review bookmark."""

    if bookmark_managed:
        if not is_review_bookmark(bookmark, prefix=prefix):
            return None
    elif not cleanup_user_bookmarks:
        return None
    if not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return ReviewBookmarkCleanupPlan(
            action=LandAction(
                kind="local bookmark",
                body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
                status="blocked",
            ),
            bookmark=bookmark,
            can_forget=False,
            change_id=change_id,
        )
    local_target = bookmark_state.local_target
    if local_target is None:
        return None
    if local_target != commit_id:
        return ReviewBookmarkCleanupPlan(
            action=LandAction(
                kind="local bookmark",
                body=(
                    t"cannot forget {ui.bookmark(bookmark)} because it already points "
                    t"to a different revision"
                ),
                status="blocked",
            ),
            bookmark=bookmark,
            can_forget=False,
            change_id=change_id,
        )
    return ReviewBookmarkCleanupPlan(
        action=LandAction(
            kind="local bookmark",
            body=t"forget {ui.bookmark(bookmark)}",
            status="planned",
        ),
        bookmark=bookmark,
        can_forget=True,
        change_id=change_id,
    )


def plan_review_bookmark_cleanup_for_revisions(
    *,
    client: BookmarkStateReader,
    prefix: str,
    cleanup_bookmarks: bool,
    cleanup_user_bookmarks: bool,
    planned_revisions: tuple[LandRevision, ...],
) -> tuple[ReviewBookmarkCleanupPlan, ...]:
    """Plan which landed local review bookmarks `land` should forget."""

    if not cleanup_bookmarks:
        return ()
    cleanup_plans: list[ReviewBookmarkCleanupPlan] = []
    for landed_revision in planned_revisions:
        cleanup_plan = _plan_review_bookmark_cleanup(
            bookmark=landed_revision.bookmark,
            bookmark_managed=landed_revision.bookmark_managed,
            cleanup_user_bookmarks=cleanup_user_bookmarks,
            prefix=prefix,
            bookmark_state=client.get_bookmark_state(landed_revision.bookmark),
            change_id=landed_revision.change_id,
            commit_id=landed_revision.commit_id,
        )
        if cleanup_plan is not None:
            cleanup_plans.append(cleanup_plan)
    return tuple(cleanup_plans)
