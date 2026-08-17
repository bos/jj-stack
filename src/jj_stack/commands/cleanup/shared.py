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
from jj_stack.models.tracking import TrackedPR, TrackingState
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
    """Rendered cleanup result for the selected repo."""

    actions: tuple[CleanupAction, ...]


@dataclass(frozen=True, slots=True)
class PreparedCleanup:
    """Locally prepared cleanup inputs before any GitHub inspection."""

    context: CommandContext
    close_open_prs: bool
    # None until plain cleanup proves it needs remote or GitHub state.
    github_target: GithubTarget | UnresolvedGithubTarget | None
    dry_run: bool
    selected_change_ids: tuple[str, ...] | None
    state: TrackingState

    @property
    def remote(self) -> GitRemote | None:
        """The selected Git remote, once remote context is loaded and one resolved."""

        return self.github_target.remote if self.github_target is not None else None


@dataclass(frozen=True, slots=True)
class PreparedCleanupChange:
    """Locally prepared cleanup state for one complete tracked PR."""

    candidate: TrackedPR
    has_mutable_copy: bool
    stale_reason: str | None
