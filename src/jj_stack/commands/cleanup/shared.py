"""Shared cleanup command models, persistence, and rendering helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._close_actions import emit_action_row
from jj_stack.github.resolution import (
    GithubTarget,
    UnresolvedGithubTarget,
)
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
)
from jj_stack.ui import Message, plain_text

CleanupActionStatus = Literal["applied", "blocked", "planned", "skipped"]
type CleanupBody = Message


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """One cleanup action that was planned, applied, blocked, or skipped."""

    kind: str
    status: CleanupActionStatus
    body: CleanupBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Rendered cleanup result for the selected repository."""

    actions: tuple[CleanupAction, ...]


@dataclass(frozen=True, slots=True)
class PreparedCleanup:
    """Locally prepared cleanup inputs before any GitHub inspection."""

    context: CommandContext
    bookmark_states: dict[str, BookmarkState]
    # None until plain cleanup proves it needs remote or GitHub state.
    github_target: GithubTarget | UnresolvedGithubTarget | None
    dry_run: bool
    state: ReviewState

    @property
    def remote(self) -> GitRemote | None:
        """The selected Git remote, once remote context is loaded and one resolved."""

        return self.github_target.remote if self.github_target is not None else None


@dataclass(frozen=True, slots=True)
class PreparedCleanupChange:
    """Locally prepared cleanup state for one complete tracked review."""

    bookmark_state: BookmarkState
    change_id: str
    current_commit_id: str | None
    review_identity: ReviewIdentity
    stale_reason: str | None
    submitted_baseline: SubmittedBaseline


def _render_cleanup_action_header(*, dry_run: bool) -> str:
    """Render the cleanup action section header."""

    return "Planned cleanup actions:" if dry_run else "Applied cleanup actions:"


def _render_cleanup_postamble(*, result: CleanupResult) -> tuple[str, ...]:
    """Render cleanup lines that only depend on the completed result."""

    if not result.actions:
        return ("No cleanup actions needed.",)
    return ()


def _emit_output_lines(lines: tuple[str, ...]) -> None:
    for line in lines:
        console.output(line)


def _build_action_streamer(
    *,
    header: str,
) -> Callable[[CleanupAction], None]:
    """Print the action header once, then stream actions as they arrive."""

    header_printed = False

    def emit_action(action: CleanupAction) -> None:
        nonlocal header_printed
        if not header_printed:
            console.output(header)
            header_printed = True
        emit_action_row(kind=action.kind, status=action.status, body=action.body)

    return emit_action
