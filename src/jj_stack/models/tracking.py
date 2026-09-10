"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from jj_stack.identifiers import ChangeId, CommitId

if TYPE_CHECKING:
    from jj_stack.models.github import GithubPR


class PRIdentity(BaseModel):
    """The pull request number and head branch saved for a local change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_number: int
    head_ref: str

    def matches_pr(self, pr: GithubPR) -> bool:
        """Whether the PR number and head branch match the saved link."""

        return pr.number == self.pr_number and pr.head.ref == self.head_ref


class SubmittedBaseline(BaseModel):
    """The commit most recently submitted or explicitly linked to a pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_id: CommitId


class TrackedPR(BaseModel):
    """The identity and submitted baseline of one tracked pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_identity: PRIdentity
    submitted_baseline: SubmittedBaseline


class TrackingState(BaseModel):
    """Complete pull request records keyed by their owning change IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[8] = 8
    prs: dict[ChangeId, TrackedPR] = Field(default_factory=dict)
