"""Shared data structures for the submit command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.github import GithubPullRequest
from jj_review.models.intent import LoadedIntent, SubmitIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.bookmarks import BookmarkResolutionResult, BookmarkSource

LocalBookmarkAction = Literal["created", "moved", "unchanged"]
PullRequestAction = Literal["created", "unchanged", "updated"]
SubmitDraftMode = Literal["default", "draft", "draft_all", "publish"]
RemoteBookmarkAction = Literal["pushed", "up to date"]
PushOperation = Literal["batch", "git_update", "up_to_date"]


@dataclass(frozen=True, slots=True)
class SubmittedRevision:
    """Remote bookmark and GitHub result for one revision in the submitted stack."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    commit_id: str
    local_action: LocalBookmarkAction
    native_revision: LocalRevision
    pull_request_action: PullRequestAction
    pull_request_is_draft: bool | None
    pull_request_number: int | None
    pull_request_title: str | None
    pull_request_url: str | None
    remote_action: RemoteBookmarkAction
    subject: str


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Remote bookmark and pull request state for the selected stack."""

    client: JjClient
    dry_run: bool
    remote: GitRemote
    revisions: tuple[SubmittedRevision, ...]
    selected_change_id: str
    selected_revset: str
    selected_subject: str
    trunk_change_id: str
    trunk_branch: str
    trunk: LocalRevision
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class GeneratedDescription:
    """Generated title/body pair for a pull request or stack summary."""

    body: str
    title: str


@dataclass(frozen=True, slots=True)
class PreparedSubmitRevision:
    """Local submit state gathered before remote and GitHub mutation."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    expected_remote_target: str | None
    local_action: LocalBookmarkAction
    push_operation: PushOperation
    remote_action: RemoteBookmarkAction
    revision: LocalRevision


@dataclass(frozen=True, slots=True)
class PendingPullRequestSync:
    """One queued PR sync task."""

    base_branch: str
    discovered_pull_request: GithubPullRequest | None
    generated_description: GeneratedDescription
    parent_change_id: str | None
    prepared_revision: PreparedSubmitRevision
    stack_head_change_id: str | None


@dataclass(frozen=True, slots=True)
class PendingStackCommentSync:
    """One queued stack-comment sync task."""

    cached_change: CachedChange
    change_id: str
    navigation_comment_body: str | None
    overview_comment_body: str | None
    pull_request_number: int


@dataclass(frozen=True, slots=True)
class PreparedSubmitInputs:
    """Local submit inputs prepared before GitHub mutations begin."""

    bookmark_states: dict[str, BookmarkState]
    bookmark_result: BookmarkResolutionResult
    client: JjClient
    generated_pull_request_descriptions: dict[str, GeneratedDescription]
    generated_stack_description: GeneratedDescription | None
    remote: GitRemote
    stack: LocalStack
    state: ReviewState


@dataclass(frozen=True, slots=True)
class SubmitIntentState:
    """Prepared submit intent bookkeeping for resumable runs."""

    intent: SubmitIntent
    intent_path: Path | None
    stale_intents: list[LoadedIntent]


class PrivateCommitFinder(Protocol):
    """Subset of the jj client interface needed for git.private-commits checks."""

    def find_private_commits(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> tuple[LocalRevision, ...]:
        """Return the revisions blocked by the repo's private-commit policy."""


class RemoteBookmarkSyncer(Protocol):
    """Subset of the jj client interface needed for remote bookmark updates."""

    def push_bookmarks(self, *, remote: str, bookmarks: tuple[str, ...]) -> None:
        """Push a batch of bookmarks to the selected remote."""

    def update_untracked_remote_bookmark(
        self,
        *,
        remote: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        """Update an existing untracked remote bookmark without importing it first."""


class InterruptedRemoteBookmarkRepairer(Protocol):
    """Subset of the jj client interface needed for stale remote bookmark repair."""

    def fetch_remote(self, *, remote: str) -> None:
        """Refresh remembered remote bookmark state for the selected remote."""

    def list_bookmark_states(
        self,
        bookmarks: tuple[str, ...] | None = None,
    ) -> dict[str, BookmarkState]:
        """Return local and remote state for the requested bookmark names."""

    def track_bookmark(self, *, remote: str, bookmark: str) -> None:
        """Track an existing remote bookmark locally."""
