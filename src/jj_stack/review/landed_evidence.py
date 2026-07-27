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
ExactSnapshotState = Literal[
    "head_mismatch",
    "identity_mismatch",
    "landed",
    "not_on_trunk",
    "unresolved",
]
RewrittenResultState = Literal[
    "head_mismatch",
    "identity_mismatch",
    "landed",
    "merge_result_missing",
    "merge_result_not_on_trunk",
    "merge_result_unresolved",
    "not_merged",
]
LandedEvidenceKind = Literal["exact", "rewritten"]


@dataclass(frozen=True, slots=True)
class LandedReviewCandidate:
    """One complete tracked review considered for landed handling."""

    change_id: str
    review_identity: ReviewIdentity
    submitted_baseline: SubmittedBaseline


@dataclass(frozen=True, slots=True)
class ExactSnapshotEvidence:
    """Repository-wide evidence for one exact submitted snapshot."""

    state: ExactSnapshotState
    reason: Message | None = None


@dataclass(frozen=True, slots=True)
class RewrittenResultEvidence:
    """Selected-path evidence for one forge-rewritten merge result."""

    state: RewrittenResultState
    merge_commit_id: str | None = None
    reason: Message | None = None


def holds_unpublished_edit(
    *,
    published_commit_ids: tuple[str, ...],
    revision: LocalRevision | None,
) -> bool:
    """Whether a local revision holds work that was never sent for review.

    This is the only authority for that question, because acting on a wrong answer destroys
    local work. An absent revision has nothing to lose and an immutable one cannot have been
    edited locally. The published set is normally just the submitted baseline; adopting a
    native survivor also counts the exact commit GitHub reported for it.
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
) -> ExactSnapshotEvidence:
    """Classify the repository-wide exact-snapshot gate without lifecycle policy."""

    if ancestry != "on_trunk":
        return ExactSnapshotEvidence(state=ancestry)
    mismatch = _identity_mismatch(candidate, pull_request, repository)
    if mismatch is not None:
        return ExactSnapshotEvidence(state="identity_mismatch", reason=mismatch)
    if pull_request.head.sha != candidate.submitted_baseline.commit_id:
        return ExactSnapshotEvidence(
            state="head_mismatch",
            reason=t"PR #{pull_request.number} no longer reports the submitted head",
        )
    return ExactSnapshotEvidence(state="landed")


def classify_rewritten_result(
    *,
    candidate: LandedReviewCandidate,
    merge_result_ancestry: CommitAncestry | None,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
) -> RewrittenResultEvidence:
    """Classify merge-result evidence for one currently selected review."""

    mismatch = _identity_mismatch(candidate, pull_request, repository)
    if mismatch is not None:
        return RewrittenResultEvidence(state="identity_mismatch", reason=mismatch)
    if pull_request.head.sha != candidate.submitted_baseline.commit_id:
        return RewrittenResultEvidence(
            state="head_mismatch",
            reason=t"PR #{pull_request.number} no longer reports the submitted head",
        )
    if pull_request.normalize_state().state != "merged":
        return RewrittenResultEvidence(state="not_merged")
    merge_commit_id = pull_request.merge_commit_sha
    if merge_commit_id is None:
        return RewrittenResultEvidence(
            state="merge_result_missing",
            reason=t"GitHub did not report the merge-result commit for PR #{pull_request.number}",
        )
    if merge_result_ancestry == "unresolved":
        return RewrittenResultEvidence(
            state="merge_result_unresolved",
            merge_commit_id=merge_commit_id,
            reason=t"merge result {ui.commit_id(merge_commit_id)} is unavailable locally",
        )
    if merge_result_ancestry != "on_trunk":
        return RewrittenResultEvidence(
            state="merge_result_not_on_trunk",
            merge_commit_id=merge_commit_id,
            reason=t"merge result {ui.commit_id(merge_commit_id)} is not on fetched trunk",
        )
    return RewrittenResultEvidence(state="landed", merge_commit_id=merge_commit_id)


def collect_landed_evidence(
    *,
    candidate: LandedReviewCandidate,
    context: CommandContext,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
    trunk_commit_id: str,
) -> tuple[ExactSnapshotEvidence, RewrittenResultEvidence]:
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


def _identity_mismatch(
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
    return None
