"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from jj_stack.models.github import GithubPullRequest


class ReviewIdentity(BaseModel):
    """Pinned nominal identity for one review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[3] = 3
    repository_owner: str
    repository_name: str
    pr_number: int
    head_owner: str
    head_ref: str

    @property
    def repository_key(self) -> tuple[str, str]:
        """Return the case-insensitive nominal repository identity."""

        return (
            self.repository_owner.casefold(),
            self.repository_name.casefold(),
        )

    def matches_pull_request(self, pull_request: GithubPullRequest) -> bool:
        """Whether live GitHub data is the exact pull request saved by this identity."""

        return (
            pull_request.number == self.pr_number
            and pull_request.head.ref == self.head_ref
            and pull_request.head.label == f"{self.head_owner}:{self.head_ref}"
        )


class SubmittedBaseline(BaseModel):
    """Exact snapshot most recently acknowledged for one review identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    commit_id: str


class ReviewState(BaseModel):
    """Validated review tracking records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[4] = 4
    review_identities: dict[str, ReviewIdentity] = Field(default_factory=dict)
    submitted_baselines: dict[str, SubmittedBaseline] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_complete_pairs(self) -> Self:
        if self.review_identities.keys() != self.submitted_baselines.keys():
            raise ValueError(
                "Review identities and submitted baselines must have identical keys."
            )
        return self

    def tracked_review(self, change_id: str) -> TrackedReview | None:
        """Return one tracked review, or None when the change is untracked."""

        identity = self.review_identities.get(change_id)
        if identity is None:
            return None
        return TrackedReview(
            change_id=change_id,
            review_identity=identity,
            submitted_baseline=self.submitted_baselines[change_id],
        )

    def tracked_reviews(self) -> tuple[TrackedReview, ...]:
        """Return every tracked review in stable change-ID order."""

        return tuple(
            TrackedReview(
                change_id=change_id,
                review_identity=self.review_identities[change_id],
                submitted_baseline=self.submitted_baselines[change_id],
            )
            for change_id in sorted(self.review_identities)
        )


class TrackedReview(BaseModel):
    """One change whose review is tracked by both of its records.

    Commands act on a review only when its identity and its submitted baseline are both present,
    so this pairs them once instead of each caller re-checking for the halves.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_id: str
    review_identity: ReviewIdentity
    submitted_baseline: SubmittedBaseline

    def matches_snapshot(
        self, pull_request: GithubPullRequest, *, repository_key: tuple[str, str]
    ) -> bool:
        """Whether live GitHub data matches this exact saved review snapshot."""

        return (
            self.review_identity.repository_key == repository_key
            and self.review_identity.matches_pull_request(pull_request)
            and pull_request.head.sha == self.submitted_baseline.commit_id
        )
