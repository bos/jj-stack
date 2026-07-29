"""Pure, distinct classifications for exact and forge-rewritten landed reviews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision
from jj_stack.ui import Message

CommitAncestry = Literal["not_on_trunk", "on_trunk", "unresolved"]
LandedEvidenceKind = Literal["exact", "rewritten"]


@dataclass(frozen=True, slots=True)
class LandedReviewCandidate:
    """One complete tracked review considered for landed handling."""

    change_id: str
    review_identity: ReviewIdentity
    submitted_baseline: SubmittedBaseline


@dataclass(frozen=True, slots=True)
class TrunkEvidence:
    """Whether one review's work is proven to be on trunk, and why not when it is not.

    Callers only ever ask whether the work is proven and, failing that, what to tell the user, so
    an unproven verdict always carries a reason. `review_mismatch` marks the one distinction a
    caller draws beyond that: the saved review no longer describes the live pull request, which is
    a tracking problem rather than a question about trunk.
    """

    on_trunk: bool
    reason: Message | None = None
    review_mismatch: bool = False
    merge_commit_id: str | None = None

    @classmethod
    def proven(cls, *, merge_commit_id: str | None = None) -> TrunkEvidence:
        return cls(on_trunk=True, merge_commit_id=merge_commit_id)

    @classmethod
    def unproven(
        cls,
        reason: Message,
        *,
        merge_commit_id: str | None = None,
        review_mismatch: bool = False,
    ) -> TrunkEvidence:
        return cls(
            on_trunk=False,
            reason=reason,
            review_mismatch=review_mismatch,
            merge_commit_id=merge_commit_id,
        )


def holds_unpublished_edit(
    *,
    published_commit_ids: tuple[str, ...],
    revision: LocalRevision | None,
) -> bool:
    """Whether a local revision holds work that was never sent for review.

    Every caller uses this check because acting on a wrong answer destroys local work. An
    absent revision has nothing to lose and an immutable one cannot have been edited locally.
    The published set is normally just the submitted baseline; adopting a native survivor
    also counts the exact commit GitHub reported for it.
    """

    return (
        revision is not None
        and not revision.immutable
        and revision.commit_id not in published_commit_ids
    )


def candidate_for_change(state: ReviewState, change_id: str) -> LandedReviewCandidate | None:
    """Return one complete active saved review, if present."""

    identity = state.review_identities.get(change_id)
    baseline = state.submitted_baselines.get(change_id)
    if identity is None or baseline is None:
        return None
    return LandedReviewCandidate(
        change_id=change_id,
        review_identity=identity,
        submitted_baseline=baseline,
    )


def complete_review_candidates(state: ReviewState) -> tuple[LandedReviewCandidate, ...]:
    """Return complete active saved reviews in stable change-ID order."""

    return tuple(
        candidate
        for change_id in sorted(state.review_identities)
        if (candidate := candidate_for_change(state, change_id)) is not None
    )


def classify_commit_ancestries(
    *,
    commit_ids: tuple[str | None, ...],
    context: CommandContext,
    trunk_commit_id: str,
) -> dict[str, CommitAncestry]:
    """Classify commits in one scan while keeping unavailable commits distinct."""

    present_commit_ids = tuple(commit_id for commit_id in commit_ids if commit_id is not None)
    memberships = context.jj_client.query_present_commit_ancestor_membership(
        present_commit_ids,
        descendant_commit_id=trunk_commit_id,
    )
    states: dict[bool, CommitAncestry] = {True: "on_trunk", False: "not_on_trunk"}
    return {
        commit_id: states[memberships[commit_id]] if commit_id in memberships else "unresolved"
        for commit_id in dict.fromkeys(present_commit_ids)
    }


def classify_exact_snapshot(
    *,
    ancestry: CommitAncestry,
    candidate: LandedReviewCandidate,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
) -> TrunkEvidence:
    """Classify the repository-wide exact-snapshot gate without lifecycle policy."""

    if ancestry != "on_trunk":
        return TrunkEvidence.unproven(
            _ancestry_reason(ancestry, candidate.submitted_baseline.commit_id)
        )
    mismatch = _snapshot_mismatch(candidate, pull_request, repository)
    if mismatch is not None:
        return TrunkEvidence.unproven(mismatch, review_mismatch=True)
    return TrunkEvidence.proven()


def classify_rewritten_result(
    *,
    candidate: LandedReviewCandidate,
    merge_result_ancestry: CommitAncestry | None,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
) -> TrunkEvidence:
    """Classify merge-result evidence for one currently selected review."""

    mismatch = _snapshot_mismatch(candidate, pull_request, repository)
    if mismatch is not None:
        return TrunkEvidence.unproven(mismatch, review_mismatch=True)
    lifecycle = pull_request.normalize_state().state
    if lifecycle != "merged":
        return TrunkEvidence.unproven(
            t"PR #{pull_request.number} is {lifecycle} without a result on trunk"
        )
    merge_commit_id = pull_request.merge_commit_sha
    if merge_commit_id is None:
        return TrunkEvidence.unproven(
            t"GitHub did not report the merge-result commit for PR #{pull_request.number}"
        )
    if merge_result_ancestry == "unresolved":
        return TrunkEvidence.unproven(
            t"merge result {ui.commit_id(merge_commit_id)} is unavailable locally",
            merge_commit_id=merge_commit_id,
        )
    if merge_result_ancestry != "on_trunk":
        return TrunkEvidence.unproven(
            t"merge result {ui.commit_id(merge_commit_id)} is not on fetched trunk",
            merge_commit_id=merge_commit_id,
        )
    return TrunkEvidence.proven(merge_commit_id=merge_commit_id)


def collect_landed_evidence(
    *,
    candidate: LandedReviewCandidate,
    context: CommandContext,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
    trunk_commit_id: str,
) -> tuple[TrunkEvidence, TrunkEvidence]:
    """Collect both distinct classifications from one current PR and trunk."""

    ancestries = classify_commit_ancestries(
        commit_ids=(candidate.submitted_baseline.commit_id, pull_request.merge_commit_sha),
        context=context,
        trunk_commit_id=trunk_commit_id,
    )
    exact = classify_exact_snapshot(
        ancestry=ancestries[candidate.submitted_baseline.commit_id],
        candidate=candidate,
        pull_request=pull_request,
        repository=repository,
    )
    rewritten = classify_rewritten_result(
        candidate=candidate,
        merge_result_ancestry=ancestries.get(pull_request.merge_commit_sha or ""),
        pull_request=pull_request,
        repository=repository,
    )
    return exact, rewritten


def _ancestry_reason(ancestry: CommitAncestry, commit_id: str) -> Message:
    if ancestry == "unresolved":
        return t"the submitted commit {ui.commit_id(commit_id)} is unavailable locally"
    return t"the submitted commit {ui.commit_id(commit_id)} is not on fetched trunk"


def _snapshot_mismatch(
    candidate: LandedReviewCandidate,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
) -> Message | None:
    identity = candidate.review_identity
    if identity.repository_key != repository.repository_key or not identity.matches_pull_request(
        pull_request
    ):
        return (
            t"PR #{pull_request.number} no longer matches the pull request recorded for "
            t"{ui.change_id(candidate.change_id)}"
        )
    if pull_request.head.sha != candidate.submitted_baseline.commit_id:
        return t"PR #{pull_request.number} no longer reports the submitted head"
    return None
