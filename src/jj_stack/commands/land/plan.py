"""Planning helpers for the land command."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.github import GithubPullRequest
from jj_stack.review.bookmarks import (
    bookmark_cleanup_allowed,
    classify_local_bookmark_forget,
    local_bookmark_forget_blocked_body,
)
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
)
from jj_stack.review.status import (
    PreparedRevision,
    PreparedStatus,
    ReviewStatusRevision,
    StatusResult,
)
from jj_stack.ui import Message

from .models import (
    LandAction,
    LandPlan,
    LandRevision,
    LandVia,
    ReviewBookmarkCleanupPlan,
)

_DivergenceKind = Literal["in_sync", "diff_equivalent", "content_divergent"]


@dataclass(frozen=True, slots=True)
class _LandabilityDecision:
    boundary_message: Message | None
    needs_resubmit: bool = False


@dataclass(frozen=True, slots=True)
class _LandPathRevision:
    """One prepared land revision with its derived review status."""

    prepared_revision: PreparedRevision
    revision: ReviewStatusRevision
    status: ReviewChangeStatus

    @property
    def bookmark_managed(self) -> bool:
        cached_change = self.revision.cached_change
        if cached_change is not None:
            return cached_change.manages_bookmark
        return self.revision.bookmark_source != "matched"

    @property
    def local_commit_id(self) -> str:
        return self.prepared_revision.revision.commit_id

    @property
    def pull_request(self) -> GithubPullRequest | None:
        lookup = self.revision.pull_request_lookup
        if lookup is None:
            return None
        return lookup.pull_request

    @property
    def remote_target(self) -> str | None:
        remote_state = self.revision.remote_state
        if remote_state is None:
            return None
        return remote_state.target


def build_land_plan(
    *,
    bypass_readiness: bool,
    client: JjClient,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
    trunk_branch: str,
    via: LandVia,
) -> LandPlan:
    path_revisions = _resolve_land_path_revisions(
        prepared_status=prepared_status,
        status_result=status_result,
    )
    planned_revisions, boundary_action = _collect_landable_prefix(
        bypass_readiness=bypass_readiness,
        client=client,
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
        push_trunk=bool(planned_revisions) and via == "push",
        trunk_branch=trunk_branch,
        via=via,
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
    with ThreadPoolExecutor(max_workers=2) as pool:
        local_future = pool.submit(client.get_commit_diff, local_commit_id)
        remote_future = pool.submit(client.get_commit_diff, remote_target)
        local_diff = local_future.result()
        remote_diff = remote_future.result()
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
    client: JjClient,
    path_revisions: tuple[tuple[PreparedRevision, ReviewStatusRevision], ...],
) -> tuple[tuple[LandRevision, ...], LandAction | None]:
    planned_revisions: list[LandRevision] = []
    for prepared_revision, revision in path_revisions:
        land_revision = _land_path_revision(
            prepared_revision=prepared_revision,
            revision=revision,
        )
        decision = _landability_decision(
            bypass_readiness=bypass_readiness,
            client=client,
            land_revision=land_revision,
        )
        if decision.boundary_message is not None:
            return tuple(planned_revisions), LandAction(
                kind="boundary",
                body=decision.boundary_message,
                status="blocked" if not planned_revisions else "planned",
            )
        pull_request = land_revision.pull_request
        if pull_request is None:
            raise AssertionError("Landable revisions require resolved pull requests.")
        planned_revisions.append(
            LandRevision(
                bookmark=revision.bookmark,
                bookmark_managed=land_revision.bookmark_managed,
                change_id=revision.change_id,
                commit_id=land_revision.local_commit_id,
                needs_resubmit=decision.needs_resubmit,
                pull_request_number=pull_request.number,
                subject=revision.subject,
            )
        )
    return tuple(planned_revisions), None


def _land_path_revision(
    *,
    prepared_revision: PreparedRevision,
    revision: ReviewStatusRevision,
) -> _LandPathRevision:
    return _LandPathRevision(
        prepared_revision=prepared_revision,
        revision=revision,
        status=classify_review_status_revision(revision),
    )


def _landability_decision(
    *,
    bypass_readiness: bool,
    client: JjClient,
    land_revision: _LandPathRevision,
) -> _LandabilityDecision:
    revision = land_revision.revision
    change_status = land_revision.status
    if land_revision.prepared_revision.revision.conflict:
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"this change still has unresolved conflicts"
            )
        )
    if change_status.link == "unlinked":
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"this change is unlinked from review tracking; run {ui.cmd('relink')} first"
            )
        )
    if change_status.local == "divergent":
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"multiple visible revisions still share that change ID"
            )
        )
    pull_request_lookup = revision.pull_request_lookup
    if pull_request_lookup is None:
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"GitHub pull request state is unavailable"
            )
        )
    if change_status.pr_lifecycle == "open":
        pull_request = land_revision.pull_request
        if pull_request is None:
            raise AssertionError("Open land boundary requires a pull request payload.")
        if change_status.pr_review_decision_error is not None:
            detail = change_status.pr_review_decision_error
            return _LandabilityDecision(
                boundary_message=(
                    t"before {revision.subject} {ui.change_id(revision.change_id)} "
                    t"because {detail}"
                )
            )
        divergence = _classify_revision_divergence(
            client=client,
            local_commit_id=land_revision.local_commit_id,
            remote_target=land_revision.remote_target,
        )
        if divergence == "content_divergent":
            return _LandabilityDecision(
                boundary_message=(
                    t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                    t"the local change differs from what reviewers approved; rerun "
                    t"{ui.cmd('submit')} to update the PR and request re-review"
                )
            )
        if change_status.pr_draft is True:
            if bypass_readiness:
                return _LandabilityDecision(
                    boundary_message=None,
                    needs_resubmit=divergence == "diff_equivalent",
                )
            return _LandabilityDecision(
                boundary_message=(
                    t"before {revision.subject} {ui.change_id(revision.change_id)} "
                    t"because PR #{pull_request.number} is still a draft"
                )
            )
        if change_status.pr_review_decision == "changes_requested":
            if bypass_readiness:
                return _LandabilityDecision(
                    boundary_message=None,
                    needs_resubmit=divergence == "diff_equivalent",
                )
            return _LandabilityDecision(
                boundary_message=(
                    t"before {revision.subject} {ui.change_id(revision.change_id)} "
                    t"because PR #{pull_request.number} has changes requested"
                )
            )
        if change_status.pr_review_decision != "approved":
            if bypass_readiness:
                return _LandabilityDecision(
                    boundary_message=None,
                    needs_resubmit=divergence == "diff_equivalent",
                )
            return _LandabilityDecision(
                boundary_message=(
                    t"before {revision.subject} {ui.change_id(revision.change_id)} "
                    t"because PR #{pull_request.number} is not approved"
                )
            )
        return _LandabilityDecision(
            boundary_message=None,
            needs_resubmit=divergence == "diff_equivalent",
        )
    if change_status.pr_lifecycle == "missing":
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"GitHub no longer reports a pull request for its branch; run "
                t"{ui.cmd('view --fetch')} or {ui.cmd('relink')} first"
            )
        )
    if change_status.pr_lifecycle == "ambiguous":
        detail = pull_request_lookup.message or "GitHub reports an ambiguous PR link"
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"{detail} Run {ui.cmd('view --fetch')} and repair the PR link with "
                t"{ui.cmd('relink')}."
            )
        )
    if change_status.has_pull_request_lookup_failure:
        detail = pull_request_lookup.message or "GitHub lookup failed"
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because {detail}"
            )
        )
    pull_request = land_revision.pull_request
    if pull_request is None:
        raise AssertionError("Closed land boundary requires a pull request payload.")
    if pull_request.state == "merged":
        return _LandabilityDecision(
            boundary_message=(
                t"before {revision.subject} {ui.change_id(revision.change_id)} because "
                t"PR #{pull_request.number} is already merged; run "
                t"{ui.cmd('cleanup --rebase')} first"
            )
        )
    return _LandabilityDecision(
        boundary_message=(
            t"before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"PR #{pull_request.number} is closed without merge"
        )
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

    if not bookmark_cleanup_allowed(
        bookmark=bookmark,
        bookmark_managed=bookmark_managed,
        cleanup_user_bookmarks=cleanup_user_bookmarks,
        prefix=prefix,
    ):
        return None
    match classify_local_bookmark_forget(
        bookmark_state=bookmark_state,
        expected_commit_id=commit_id,
    ):
        case "absent":
            return None
        case "conflicted" | "diverged" as safety:
            return ReviewBookmarkCleanupPlan(
                action=LandAction(
                    kind="local bookmark",
                    body=local_bookmark_forget_blocked_body(bookmark, safety),
                    status="blocked",
                ),
                bookmark=bookmark,
                can_forget=False,
                change_id=change_id,
            )
        case _:
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
    bookmark_states: dict[str, BookmarkState],
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
            bookmark_state=bookmark_states.get(
                landed_revision.bookmark,
                BookmarkState(name=landed_revision.bookmark),
            ),
            change_id=landed_revision.change_id,
            commit_id=landed_revision.commit_id,
        )
        if cleanup_plan is not None:
            cleanup_plans.append(cleanup_plan)
    return tuple(cleanup_plans)
