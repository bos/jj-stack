"""Shared data structures for the submit command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.branches import ResolvedReviewBranch
from jj_stack.state.store import ReviewStateStore

PullRequestAction = Literal["created", "unchanged", "updated"]
SubmitDraftMode = Literal["default", "draft", "draft_all", "open"]
RemoteBranchAction = Literal["pushed", "up to date"]


@dataclass(frozen=True, slots=True)
class SubmitOptions:
    """Parsed submit options after CLI normalization."""

    descriptions: tuple[str, ...]
    describe_with: str | None
    draft_mode: SubmitDraftMode
    dry_run: bool
    edit: bool
    existing_only: bool
    labels: list[str] | None
    re_request: bool
    reviewers: list[str] | None
    revset: str | None
    team_reviewers: list[str] | None


@dataclass(frozen=True, slots=True)
class ResolvedSubmitOptions:
    """Submit options after CLI values have been combined with config defaults."""

    labels: list[str]
    reviewers: list[str]
    team_reviewers: list[str]


@dataclass(frozen=True, slots=True)
class PreparedSubmitRevision:
    """Review branch state gathered before remote and GitHub mutation."""

    branch: str
    expected_remote_target: str | None
    remote_action: RemoteBranchAction
    revision: LocalRevision


@dataclass(frozen=True, slots=True)
class SubmittedRevision:
    """GitHub pull request result for one prepared revision in the submitted stack."""

    prepared: PreparedSubmitRevision
    pull_request_action: PullRequestAction
    pull_request_is_draft: bool | None
    pull_request_number: int | None
    pull_request_url: str | None

    @property
    def change_id(self) -> str:
        """The submitted revision's change ID."""

        return self.prepared.revision.change_id


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Remote branch and pull request state for the selected stack."""

    client: JjClient
    dry_run: bool
    revisions: tuple[SubmittedRevision, ...]
    trunk: LocalRevision


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
    draft: bool
    generated_description: GeneratedDescription
    prepared: PreparedSubmitRevision


@dataclass(frozen=True, slots=True)
class PreparedSubmitInputs:
    """Local submit inputs prepared before GitHub mutations begin."""

    branch_resolutions: tuple[ResolvedReviewBranch, ...]
    client: JjClient
    generated_pull_request_descriptions: dict[str, GeneratedDescription]
    generated_stack_description: GeneratedDescription | None
    remote: GitRemote
    stack: LocalStack
    state: ReviewState


@dataclass(slots=True)
class SubmitMutationRun:
    """Mutable submit state shared by mutation phases."""

    dry_run: bool
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
        self.state = self.state_store.relink_review(
            change_id,
            expected_identity=expected_identity,
            expected_baseline=expected_baseline,
            identity=identity,
            baseline=baseline,
        )


class PrivateCommitFinder(Protocol):
    """Subset of the jj client interface needed for git.private-commits checks."""

    def find_private_commits(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> tuple[LocalRevision, ...]:
        """Return the revisions blocked by the repo's private-commit policy."""
