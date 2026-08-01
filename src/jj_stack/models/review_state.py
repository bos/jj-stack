"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from jj_stack.models.github import GithubPullRequest

ReviewStateRecordType = Literal["review_identity", "submitted_baseline"]


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


class ReviewStateRecordIssue(BaseModel):
    """One opaque tracking record that could not be validated independently."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: ReviewStateRecordType
    change_id: str
    fingerprint: str
    validation_error: str


class ReviewState(BaseModel):
    """Validated tracking records plus non-persisted record diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[4] = 4
    review_identities: dict[str, ReviewIdentity] = Field(default_factory=dict)
    submitted_baselines: dict[str, SubmittedBaseline] = Field(default_factory=dict)
    record_issues: tuple[ReviewStateRecordIssue, ...] = Field(
        default=(),
        exclude=True,
        repr=False,
    )

    def issues_for(self, change_id: str) -> tuple[ReviewStateRecordIssue, ...]:
        """Return isolated record problems for one exact change ID."""

        return tuple(issue for issue in self.record_issues if issue.change_id == change_id)

    def tracked_review(self, change_id: str) -> TrackedReview | None:
        """Return one tracked review, or None when either record is absent."""

        identity = self.review_identities.get(change_id)
        baseline = self.submitted_baselines.get(change_id)
        if identity is None or baseline is None:
            return None
        return TrackedReview(
            change_id=change_id,
            review_identity=identity,
            submitted_baseline=baseline,
        )

    def tracked_reviews(self) -> tuple[TrackedReview, ...]:
        """Return every tracked review in stable change-ID order."""

        return tuple(
            review
            for change_id in sorted(self.review_identities)
            if (review := self.tracked_review(change_id)) is not None
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
