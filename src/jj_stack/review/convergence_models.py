"""Complete side-effect-free plans for selected convergence."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import TrackedReview
from jj_stack.models.stack import LocalRevision
from jj_stack.review.trunk_evidence import TrunkEvidenceKind


@dataclass(frozen=True, slots=True)
class FinishReview:
    candidate: TrackedReview
    pull_request: GithubPullRequest


@dataclass(frozen=True, slots=True)
class SkipReviewFinish:
    candidate: TrackedReview


type ReviewFinishPlan = FinishReview | SkipReviewFinish


@dataclass(frozen=True, slots=True)
class OnTrunkChange:
    candidate: TrackedReview
    evidence_kind: TrunkEvidenceKind
    finish: ReviewFinishPlan
    revision: LocalRevision | None


@dataclass(frozen=True, slots=True)
class ConvergenceActions:
    on_trunk: tuple[OnTrunkChange, ...]
    reviewed_survivors: tuple[LocalRevision, ...]
    survivors: tuple[LocalRevision, ...]
    working_copy_children: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdoptedSurvivor:
    candidate: TrackedReview
    local_revision: LocalRevision
    remote_commit_id: str


@dataclass(frozen=True, slots=True)
class OrdinaryConvergencePlan:
    actions: ConvergenceActions


@dataclass(frozen=True, slots=True)
class GithubStackMergePlan:
    actions: ConvergenceActions
    adopted_survivors: tuple[AdoptedSurvivor, ...]


@dataclass(frozen=True, slots=True)
class GithubStackRebasePlan:
    actions: ConvergenceActions
    adopted_survivors: tuple[AdoptedSurvivor, ...]


type SelectedConvergencePlan = (
    OrdinaryConvergencePlan | GithubStackMergePlan | GithubStackRebasePlan
)
