"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jj_stack.identifiers import ChangeId, CommitId


class PRIdentity(BaseModel):
    """The pull request number and head branch saved for a local change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_number: int
    head_ref: str


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
