"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from jj_stack.models.github import GithubPR


class PRIdentity(BaseModel):
    """Pinned nominal identity for one pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_owner: str
    repo_name: str
    pr_number: int
    head_owner: str
    head_ref: str

    @property
    def repo_key(self) -> tuple[str, str]:
        """Return the case-insensitive nominal repo identity."""

        return self.repo_owner.casefold(), self.repo_name.casefold()

    def matches_pr(self, pr: GithubPR) -> bool:
        """Whether live GitHub data is the exact pull request saved by this identity."""

        return (
            pr.number == self.pr_number
            and pr.head.ref == self.head_ref
            and pr.head.label == f"{self.head_owner}:{self.head_ref}"
        )


class SubmittedBaseline(BaseModel):
    """Exact snapshot most recently acknowledged for one pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_id: str


class TrackingState(BaseModel):
    """Validated pull request tracking records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[6] = 6
    pr_identities: dict[str, PRIdentity] = Field(default_factory=dict)
    submitted_baselines: dict[str, SubmittedBaseline] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_complete_pairs(self) -> Self:
        if self.pr_identities.keys() != self.submitted_baselines.keys():
            raise ValueError("Pull request identities and baselines must have identical keys.")
        return self

    def tracked_pr(self, change_id: str) -> TrackedPR | None:
        """Return one tracked pull request, or None when the change is untracked."""

        identity = self.pr_identities.get(change_id)
        if identity is None:
            return None
        return TrackedPR(
            change_id=change_id,
            pr_identity=identity,
            submitted_baseline=self.submitted_baselines[change_id],
        )

    def tracked_prs(self) -> tuple[TrackedPR, ...]:
        """Return every tracked pull request in stable change-ID order."""

        return tuple(
            TrackedPR(
                change_id=change_id,
                pr_identity=self.pr_identities[change_id],
                submitted_baseline=self.submitted_baselines[change_id],
            )
            for change_id in sorted(self.pr_identities)
        )


class TrackedPR(BaseModel):
    """One change whose pull request is tracked by both of its records.

    Commands act on a pull request only when its identity and submitted baseline are both
    present, so this pairs them once instead of each caller re-checking for the halves.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_id: str
    pr_identity: PRIdentity
    submitted_baseline: SubmittedBaseline

    def matches_snapshot(self, pr: GithubPR, *, repo_key: tuple[str, str]) -> bool:
        """Whether live GitHub data matches this exact saved pull request snapshot."""

        return (
            self.pr_identity.repo_key == repo_key
            and self.pr_identity.matches_pr(pr)
            and pr.head.sha == self.submitted_baseline.commit_id
        )
