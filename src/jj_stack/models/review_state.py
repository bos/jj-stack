"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LinkState = Literal["active", "unlinked"]
BookmarkOwnership = Literal["managed", "external"]
ReviewStateRecordType = Literal["review_identity", "submitted_baseline"]


class ReviewIdentity(BaseModel):
    """Pinned nominal identity for one review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    github_host: str
    repository_owner: str
    repository_name: str
    pr_number: int
    head_owner: str
    head_ref: str
    bookmark_ownership: BookmarkOwnership
    link_state: LinkState = "active"

    @property
    def is_tracked(self) -> bool:
        """Whether commands may inspect and update this review."""

        return self.link_state == "active"

    @property
    def is_unlinked(self) -> bool:
        """Whether the user explicitly detached this review."""

        return self.link_state == "unlinked"

    @property
    def manages_bookmark(self) -> bool:
        """Whether jj-stack may retire the review bookmark."""

        return self.bookmark_ownership == "managed"

    @property
    def repository_key(self) -> tuple[str, str, str]:
        """Return the case-insensitive nominal repository identity."""

        return (
            self.github_host.casefold(),
            self.repository_owner.casefold(),
            self.repository_name.casefold(),
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

    version: Literal[2] = 2
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
