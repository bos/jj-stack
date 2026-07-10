"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

LinkState = Literal["active", "unlinked"]
BookmarkOwnership = Literal["managed", "external"]
PendingDirectLandPhase = Literal["prepared", "trunk_moved"]


class PendingDirectLandRevision(BaseModel):
    """Exact review identity for one revision in a pending direct land."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bookmark: str
    bookmark_ownership: BookmarkOwnership
    change_id: str
    commit_id: str
    pull_request_number: int
    subject: str


class PendingDirectLand(BaseModel):
    """One unresolved direct-push land transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bookmark_prefix: str
    cleanup_bookmarks: bool
    cleanup_user_bookmarks: bool
    finalized_change_ids: tuple[str, ...] = ()
    github_host: str
    github_repository: str
    operation_id: str
    original_local_trunk_commit_id: str | None
    original_trunk_commit_id: str
    phase: PendingDirectLandPhase = "prepared"
    planned_revisions: tuple[PendingDirectLandRevision, ...]
    remote_name: str
    remote_url: str
    trunk_branch: str

    @model_validator(mode="after")
    def validate_revision_scope(self) -> Self:
        if not self.planned_revisions:
            raise ValueError("a pending direct land requires at least one revision")
        planned_change_ids = tuple(
            revision.change_id for revision in self.planned_revisions
        )
        if len(set(planned_change_ids)) != len(planned_change_ids):
            raise ValueError("pending direct land revisions must have unique change IDs")
        if len(set(self.finalized_change_ids)) != len(self.finalized_change_ids):
            raise ValueError("finalized pending direct land change IDs must be unique")
        if set(self.finalized_change_ids) - set(planned_change_ids):
            raise ValueError("finalized changes must belong to the pending direct land")
        return self

    @property
    def target_trunk_commit_id(self) -> str:
        """Return the exact commit the transaction moves trunk to."""

        return self.planned_revisions[-1].commit_id


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
        """Whether jj-stack should clean up this bookmark automatically."""

        return self.bookmark_ownership == "managed"

    def with_cleared_comments(self) -> CachedChange:
        """Return this record without saved managed-comment identities."""

        return self.model_copy(
            update={
                "navigation_comment_id": None,
                "overview_comment_id": None,
            }
        )

    def with_cleared_pr_identity(self) -> CachedChange:
        """Return this record without saved pull-request identity."""

        return self.model_copy(
            update={
                "pr_is_draft": None,
                "pr_number": None,
                "pr_review_decision": None,
                "pr_state": None,
                "pr_url": None,
            }
        )


class ReviewState(BaseModel):
    """Saved tracking data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    changes: dict[str, CachedChange] = Field(default_factory=dict)
    pending_direct_land: PendingDirectLand | None = None
