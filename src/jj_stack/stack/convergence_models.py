"""Complete side-effect-free plans for selected convergence."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.trunk_evidence import TrunkEvidenceKind


@dataclass(frozen=True, slots=True)
class FinishPR:
    candidate: TrackedPR
    pr: GithubPR


@dataclass(frozen=True, slots=True)
class SkipPRFinish:
    candidate: TrackedPR


type PRFinishPlan = FinishPR | SkipPRFinish


@dataclass(frozen=True, slots=True)
class OnTrunkChange:
    candidate: TrackedPR
    evidence_kind: TrunkEvidenceKind
    finish: PRFinishPlan
    change: LocalCommit | None


@dataclass(frozen=True, slots=True)
class ConvergenceActions:
    on_trunk: tuple[OnTrunkChange, ...]
    submitted_survivors: tuple[LocalCommit, ...]
    survivors: tuple[LocalCommit, ...]
    working_copy_children: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdoptedSurvivor:
    candidate: TrackedPR
    local_change: LocalCommit
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
