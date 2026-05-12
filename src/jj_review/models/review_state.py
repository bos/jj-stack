"""Typed models for jj-review tracking data."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LinkState = Literal["active", "unlinked"]
BookmarkOwnership = Literal["managed", "external"]


class CachedChange(BaseModel):
    """Tracking data for one logical `jj` change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bookmark: str | None = None
    bookmark_ownership: BookmarkOwnership = "managed"
    last_submitted_commit_id: str | None = None
    last_submitted_parent_change_id: str | None = None
    last_submitted_stack_head_change_id: str | None = None
    link_state: LinkState = "active"
    pr_is_draft: bool | None = None
    pr_number: int | None = None
    pr_review_decision: str | None = None
    pr_state: str | None = None
    pr_url: str | None = None
    navigation_comment_id: int | None = None
    overview_comment_id: int | None = None

    @property
    def has_review_identity(self) -> bool:
        """Whether tracking state proves this change was attached to review before."""

        return any(
            value is not None
            for value in (
                self.last_submitted_commit_id,
                self.pr_number,
                self.pr_review_decision,
                self.pr_state,
                self.pr_url,
                self.navigation_comment_id,
                self.overview_comment_id,
            )
        )

    @property
    def is_tracked(self) -> bool:
        """Whether this change is actively tracked for review."""

        return self.link_state == "active" and self.has_review_identity

    @property
    def is_unlinked(self) -> bool:
        """Whether this change has been intentionally unlinked from review tracking."""

        return self.link_state == "unlinked"

    @property
    def manages_bookmark(self) -> bool:
        """Whether jj-review should clean up this bookmark automatically."""

        return self.bookmark_ownership == "managed"


class ReviewState(BaseModel):
    """Saved tracking data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    changes: dict[str, CachedChange] = Field(default_factory=dict)
