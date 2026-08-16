"""Shared cleanup command models, persistence, and rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jj_stack.bootstrap import CommandContext
from jj_stack.github.resolution import (
    GithubTarget,
    UnresolvedGithubTarget,
)
from jj_stack.models.git import GitRemote
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
)
from jj_stack.ui import Message

CleanupActionStatus = Literal["applied", "blocked", "planned", "skipped"]
type CleanupBody = Message


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """One cleanup action that was planned, applied, blocked, or skipped."""

    kind: str
    status: CleanupActionStatus
    body: CleanupBody


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Rendered cleanup result for the selected repository."""

    actions: tuple[CleanupAction, ...]


@dataclass(frozen=True, slots=True)
class PreparedCleanup:
    """Locally prepared cleanup inputs before any GitHub inspection."""

    context: CommandContext
    close_open_pull_requests: bool
    # None until plain cleanup proves it needs remote or GitHub state.
    github_target: GithubTarget | UnresolvedGithubTarget | None
    dry_run: bool
    selected_change_ids: tuple[str, ...] | None
    state: ReviewState

    @property
    def remote(self) -> GitRemote | None:
        """The selected Git remote, once remote context is loaded and one resolved."""

        return self.github_target.remote if self.github_target is not None else None


@dataclass(frozen=True, slots=True)
class PreparedCleanupChange:
    """Locally prepared cleanup state for one complete tracked review."""

    change_id: str
    has_mutable_copy: bool
    review_identity: ReviewIdentity
    stale_reason: str | None
    submitted_baseline: SubmittedBaseline
