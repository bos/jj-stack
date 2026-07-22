"""Shared data structures for the submit command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.bookmarks import BookmarkSource, ResolvedBookmark
from jj_stack.review.restart import RestartedReview
from jj_stack.state.store import ReviewStateStore

LocalBookmarkAction = Literal["created", "moved", "unchanged"]
PullRequestAction = Literal["created", "unchanged", "updated"]
SubmitDraftMode = Literal["default", "draft", "draft_all", "open"]
RemoteBookmarkAction = Literal["pushed", "up to date"]
PushOperation = Literal["batch", "git_update", "up_to_date"]


@dataclass(frozen=True, slots=True)
class SubmitOptions:
    """Parsed submit options after CLI normalization."""

    descriptions: tuple[str, ...]
    describe_with: str | None
    draft_mode: SubmitDraftMode
    dry_run: bool
    edit: bool
    labels: list[str] | None
    re_request: bool
    restart: bool
    reviewers: list[str] | None
    revset: str | None
    team_reviewers: list[str] | None
    use_bookmarks: list[str] | None


@dataclass(frozen=True, slots=True)
class ResolvedSubmitOptions:
    """Submit options after CLI values have been combined with config defaults."""

    labels: list[str]
    reviewers: list[str]
    team_reviewers: list[str]
    use_bookmarks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedSubmitRevision:
    """Local submit state gathered before remote and GitHub mutation."""

    bookmark: str
    bookmark_source: BookmarkSource
    expected_remote_target: str | None
    local_action: LocalBookmarkAction
    push_operation: PushOperation
    remote_action: RemoteBookmarkAction
    revision: LocalRevision


@dataclass(frozen=True, slots=True)
class SubmittedRevision:
    """GitHub pull request result for one prepared revision in the submitted stack."""

    prepared: PreparedSubmitRevision
    pull_request_action: PullRequestAction
    pull_request_is_draft: bool | None
    pull_request_number: int | None
    pull_request_title: str | None
    pull_request_url: str | None

    @property
    def change_id(self) -> str:
        """The submitted revision's change ID."""

        return self.prepared.revision.change_id


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
class PendingPullRequestSync:
    """One queued PR sync task."""

    base_branch: str
    discovered_pull_request: GithubPullRequest | None
    generated_description: GeneratedDescription
    parent_change_id: str | None
    prepared: PreparedSubmitRevision
    stack_head_change_id: str | None


@dataclass(frozen=True, slots=True)
class PendingStackCommentSync:
    """One queued stack-comment sync task."""

    change_id: str
    navigation_comment_body: str | None
    overview_comment_body: str | None
    pull_request_number: int


@dataclass(frozen=True, slots=True)
class PreparedSubmitInputs:
    """Local submit inputs prepared before GitHub mutations begin."""

    bookmark_states: dict[str, BookmarkState]
    bookmark_resolutions: tuple[ResolvedBookmark, ...]
    client: JjClient
    generated_pull_request_descriptions: dict[str, GeneratedDescription]
    generated_stack_description: GeneratedDescription | None
    remote: GitRemote
    restarted_change_ids: frozenset[str]
    restarted_reviews: tuple[RestartedReview, ...]
    stack: LocalStack
    state: ReviewState


@dataclass(slots=True)
class SubmitMutationRun:
    """Mutable submit state shared by mutation phases."""

    dry_run: bool
    restarted_reviews: dict[str, RestartedReview]
    state: ReviewState
    state_store: ReviewStateStore

    def record_submission(
        self,
        *,
        baseline: SubmittedBaseline,
        change_id: str,
        identity: ReviewIdentity,
    ) -> None:
        """Save one GitHub-acknowledged review snapshot."""

        if self.dry_run:
            return
        restarted = self.restarted_reviews.get(change_id)
        if restarted is not None:
            self.state = self.state_store.relink_review(
                change_id,
                expected_identity=restarted.identity,
                expected_baseline=restarted.baseline,
                identity=identity,
                baseline=baseline,
            )
            return
        expected_identity = self.state.review_identities.get(change_id)
        expected_baseline = self.state.submitted_baselines.get(change_id)
        if expected_identity is None and expected_baseline is None:
            self.state = self.state_store.create_review(
                change_id,
                identity=identity,
                baseline=baseline,
            )
            return
        if expected_identity is None or expected_baseline is None:
            raise RuntimeError(f"Incomplete review state for {change_id}.")
        if identity != expected_identity:
            raise RuntimeError(f"Review identity changed during submit for {change_id}.")
        self.state = self.state_store.advance_baseline(
            change_id,
            expected_identity=expected_identity,
            expected_baseline=expected_baseline,
            baseline=baseline,
        )


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
