"""Shared data structures for the land command."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from jj_review.config import RepoConfig
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.intent import LandIntent, LoadedIntent
from jj_review.review.status import PreparedStatus
from jj_review.ui import Message, plain_text

LandActionStatus = Literal["applied", "blocked", "planned"]
DivergenceKind = Literal["in_sync", "diff_equivalent", "content_divergent"]
type LandActionBody = Message
type DivergenceClassifier = Callable[[str, str | None], DivergenceKind]


@dataclass(frozen=True, slots=True)
class LandAction:
    """One planned, applied, or blocked landing action."""

    kind: str
    body: LandActionBody
    status: LandActionStatus

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class LandResult:
    """Rendered landing result for one selected local stack."""

    actions: tuple[LandAction, ...]
    applied: bool
    bypass_readiness: bool
    blocked: bool
    github_repository: str
    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class PreparedLand:
    """Locally prepared land inputs before GitHub planning and execution."""

    cleanup_bookmarks: bool
    dry_run: bool
    bypass_readiness: bool
    config: RepoConfig
    prepared_status: PreparedStatus
    selected_pr_number: int | None


@dataclass(frozen=True, slots=True)
class LandRevision:
    """One landed change plus its GitHub link."""

    bookmark: str
    bookmark_managed: bool
    change_id: str
    commit_id: str
    needs_resubmit: bool
    pull_request_number: int
    subject: str


@dataclass(frozen=True, slots=True)
class LandPlan:
    """Resolved landing plan for the selected stack."""

    blocked: bool
    boundary_action: LandAction | None
    landed_revisions: tuple[LandRevision, ...]
    push_trunk: bool
    trunk_branch: str

    @property
    def resubmit_revisions(self) -> tuple[LandRevision, ...]:
        return tuple(revision for revision in self.landed_revisions if revision.needs_resubmit)


@dataclass(frozen=True, slots=True)
class ReviewBookmarkCleanupPlan:
    """Planned post-land cleanup for one landed local review bookmark."""

    action: LandAction
    bookmark: str
    can_forget: bool
    change_id: str


@dataclass(frozen=True, slots=True)
class ResumeLandIntent:
    """A stale land intent that still matches the current selected stack."""

    intent: LandIntent
    path: Path
    mode: Literal["exact-path", "tail-after-landed-prefix"]


@dataclass(frozen=True, slots=True)
class LandExecutionState:
    """Resolved live-run land state after resume checks."""

    execution_plan: LandPlan
    resume_intent: ResumeLandIntent | None
    stale_intents: list[LoadedIntent]
    state_dir: Path


class BookmarkStateReader(Protocol):
    """Subset of the jj client interface needed for trunk bookmark inspection."""

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        """Return local and remote state for the named bookmark."""


class BookmarkRestorer(Protocol):
    """Subset of the jj client interface needed for local trunk restoration."""

    def forget_bookmarks(self, bookmarks: Sequence[str]) -> None:
        """Forget local bookmarks."""

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        """Create or move a local bookmark."""
