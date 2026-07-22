"""Pure, distinct classifications for exact and forge-rewritten landed reviews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
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


def candidate_for_change(state: ReviewState, change_id: str) -> LandedReviewCandidate | None:
    """Return one complete active saved review, if present."""

    identity = state.review_identities.get(change_id)
    baseline = state.submitted_baselines.get(change_id)
    if identity is None or baseline is None or not identity.is_tracked:
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


def classify_commit_ancestry(
    *,
    commit_id: str,
    context: CommandContext,
    trunk_commit_id: str,
) -> CommitAncestry:
    """Keep an unavailable commit distinct from a known off-trunk commit."""

    try:
        landed = context.jj_client.query_commit_ids_ancestors_of(
            (commit_id,),
            descendant_commit_id=trunk_commit_id,
        )
    except JjCommandError:
        return "unresolved"
    return "on_trunk" if commit_id in landed else "not_on_trunk"


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

    exact = classify_exact_snapshot(
        ancestry=classify_commit_ancestry(
            commit_id=candidate.submitted_baseline.commit_id,
            context=context,
            trunk_commit_id=trunk_commit_id,
        ),
        candidate=candidate,
        pull_request=pull_request,
        repository=repository,
    )
    merge_commit_id = pull_request.merge_commit_sha
    rewritten = classify_rewritten_result(
        candidate=candidate,
        merge_result_ancestry=(
            None
            if merge_commit_id is None
            else classify_commit_ancestry(
                commit_id=merge_commit_id,
                context=context,
                trunk_commit_id=trunk_commit_id,
            )
        ),
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
    if (
        identity.github_host != repository.host
        or identity.repository_owner.casefold() != repository.owner.casefold()
        or identity.repository_name.casefold() != repository.repo.casefold()
        or pull_request.number != identity.pr_number
        or pull_request.head.ref != identity.head_ref
        or pull_request.head.label != f"{identity.head_owner}:{identity.head_ref}"
    ):
        return (
            t"live PR identity no longer matches saved review {ui.change_id(candidate.change_id)}"
        )
    return None
