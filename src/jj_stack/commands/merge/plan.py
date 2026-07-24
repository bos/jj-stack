"""Planning helpers for the merge command."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.models.github import GithubPullRequest
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
    MergeAction,
    MergePlan,
    MergeRevision,
)


@dataclass(frozen=True, slots=True)
class _MergeabilityDecision:
    boundary_message: Message | None


@dataclass(frozen=True, slots=True)
class _MergePathRevision:
    """One prepared merge revision with its derived review status."""

    prepared_revision: PreparedRevision
    revision: ReviewStatusRevision
    status: ReviewChangeStatus

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

    @property
    def submitted_baseline(self) -> str | None:
        baseline = self.revision.submitted_baseline
        if baseline is None:
            return None
        return baseline.commit_id


def build_merge_plan(
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
    trunk_branch: str,
) -> MergePlan:
    path_revisions = _resolve_merge_path_revisions(
        prepared_status=prepared_status,
        status_result=status_result,
    )
    planned_revisions, boundary_action = _collect_mergeable_prefix(path_revisions=path_revisions)

    if not planned_revisions and boundary_action is None:
        boundary_action = MergeAction(
            kind="boundary",
            body="No changes on the selected stack can be merged.",
            status="blocked",
        )
    return MergePlan(
        blocked=not planned_revisions,
        boundary_action=boundary_action,
        planned_revisions=tuple(planned_revisions),
        trunk_branch=trunk_branch,
    )


def validate_merge_plan_method(
    *,
    merge_method: str | None,
    plan: MergePlan,
) -> None:
    if merge_method != "rebase":
        return
    if len(plan.planned_revisions) <= 1:
        return
    raise CliError(
        t"A rebase merge cannot merge more than one PR at a time: GitHub rewrites "
        t"commit IDs during a rebase merge, so every later PR would replay its "
        t"ancestors' commits.",
        hint=t"Use {ui.cmd('--merge-method squash')} or merge one PR per run.",
    )


def _resolve_merge_path_revisions(
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
                f"Prepared merge revision {change_id} is missing from the status result."
            )
        path_revisions.append((prepared_revision, revision))
    return tuple(path_revisions)


def _collect_mergeable_prefix(
    *,
    path_revisions: tuple[tuple[PreparedRevision, ReviewStatusRevision], ...],
) -> tuple[tuple[MergeRevision, ...], MergeAction | None]:
    planned_revisions: list[MergeRevision] = []
    for prepared_revision, revision in path_revisions:
        merge_revision = _merge_path_revision(
            prepared_revision=prepared_revision,
            revision=revision,
        )
        decision = _mergeability_decision(merge_revision=merge_revision)
        if decision.boundary_message is not None:
            return tuple(planned_revisions), MergeAction(
                kind="boundary",
                body=decision.boundary_message,
                status="blocked" if not planned_revisions else "planned",
            )
        pull_request = merge_revision.pull_request
        if pull_request is None:
            raise AssertionError("Mergeable revisions require resolved pull requests.")
        identity = revision.review_identity
        if identity is None:
            raise AssertionError("Mergeable revisions require saved review identity.")
        planned_revisions.append(
            MergeRevision(
                base_ref=pull_request.base.ref,
                change_id=revision.change_id,
                commit_id=merge_revision.local_commit_id,
                identity=identity,
                subject=revision.subject,
            )
        )
    return tuple(planned_revisions), None


def _merge_path_revision(
    *,
    prepared_revision: PreparedRevision,
    revision: ReviewStatusRevision,
) -> _MergePathRevision:
    return _MergePathRevision(
        prepared_revision=prepared_revision,
        revision=revision,
        status=classify_review_status_revision(revision),
    )


def _mergeability_decision(
    *,
    merge_revision: _MergePathRevision,
) -> _MergeabilityDecision:
    revision = merge_revision.revision
    change_status = merge_revision.status
    if merge_revision.prepared_revision.revision.conflict:
        return _merge_boundary(
            revision,
            "this change still has unresolved conflicts",
        )
    if change_status.link == "unlinked":
        return _merge_boundary(
            revision,
            t"this change is unlinked from review tracking; run {ui.cmd('relink')} first",
        )
    if change_status.local == "divergent":
        return _merge_boundary(
            revision,
            "multiple visible revisions still share that change ID",
        )
    pull_request_lookup = revision.pull_request_lookup
    if pull_request_lookup is None:
        return _merge_boundary(revision, "GitHub pull request state is unavailable")
    if change_status.pr_lifecycle == "open":
        return _open_mergeability_decision(merge_revision=merge_revision)
    return _closed_mergeability_decision(merge_revision)


def _open_mergeability_decision(
    *,
    merge_revision: _MergePathRevision,
) -> _MergeabilityDecision:
    revision = merge_revision.revision
    change_status = merge_revision.status
    pull_request = merge_revision.pull_request
    if pull_request is None:
        raise AssertionError("Open merge boundary requires a pull request payload.")
    projection_targets = (
        merge_revision.local_commit_id,
        merge_revision.submitted_baseline,
        merge_revision.remote_target,
        pull_request.head.sha,
    )
    if any(target != merge_revision.local_commit_id for target in projection_targets):
        return _merge_boundary(
            revision,
            t"the local change, last submitted version, review branch, and pull request do "
            t"not all identify the same exact commit; run "
            t"{ui.cmd(f'jj-stack submit {revision.change_id}')} before merging",
        )
    if change_status.pr_draft is True:
        return _merge_boundary(revision, t"PR #{pull_request.number} is still a draft")
    return _MergeabilityDecision(boundary_message=None)


def _closed_mergeability_decision(
    merge_revision: _MergePathRevision,
) -> _MergeabilityDecision:
    revision = merge_revision.revision
    change_status = merge_revision.status
    pull_request_lookup = revision.pull_request_lookup
    if pull_request_lookup is None:
        raise AssertionError("Closed merge boundary requires a pull request lookup.")
    if change_status.pr_lifecycle == "missing":
        return _merge_boundary(
            revision,
            t"GitHub no longer reports a pull request for its branch; run "
            t"{ui.cmd('view --fetch')} or {ui.cmd('relink')} first",
        )
    if change_status.pr_lifecycle == "ambiguous":
        detail = pull_request_lookup.message or "GitHub reports an ambiguous PR link"
        return _merge_boundary(
            revision,
            t"{detail} Run {ui.cmd('view --fetch')} and repair the PR link with "
            t"{ui.cmd('relink')}.",
        )
    if change_status.has_pull_request_lookup_failure:
        detail = pull_request_lookup.message or "GitHub lookup failed"
        return _merge_boundary(revision, detail)
    pull_request = merge_revision.pull_request
    if pull_request is None:
        raise AssertionError("Closed merge boundary requires a pull request payload.")
    if pull_request.state == "merged":
        return _merge_boundary(
            revision,
            t"PR #{pull_request.number} is already merged; preview "
            t"{ui.cmd('jj-stack sync --dry-run')} {ui.change_id(revision.change_id)} before "
            t"running {ui.cmd('jj-stack sync')} {ui.change_id(revision.change_id)}",
        )
    return _merge_boundary(
        revision,
        t"PR #{pull_request.number} is closed without merge",
    )


def _merge_boundary(
    revision: ReviewStatusRevision,
    reason: Message,
) -> _MergeabilityDecision:
    return _MergeabilityDecision(
        boundary_message=(
            t"before {revision.subject} {ui.change_id(revision.change_id)} because ",
            reason,
        )
    )
