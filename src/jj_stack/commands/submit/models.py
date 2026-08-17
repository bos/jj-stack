"""Shared data structures for the submit command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple, Protocol

from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.stack.pr_branches import ResolvedPRBranch
from jj_stack.state.store import TrackingStore

PRAction = Literal["created", "unchanged", "updated"]
PRDraftAction = Literal["draft", "ready"]
SubmitDraftMode = Literal["default", "draft", "draft_all", "open"]
RemoteBranchAction = Literal["pushed", "up to date"]


@dataclass(frozen=True, slots=True)
class SubmitOptions:
    """Parsed submit options after CLI normalization."""

    base_revset: str | None
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
class PreparedSubmitChange:
    """PR branch state gathered before remote and GitHub mutation."""

    branch: str
    expected_remote_target: str | None
    remote_action: RemoteBranchAction
    change: LocalCommit


@dataclass(frozen=True, slots=True)
class SubmittedChange:
    """GitHub pull request result for one prepared change in the submitted stack."""

    prepared: PreparedSubmitChange
    pr_action: PRAction
    pr_is_draft: bool | None
    pr_number: int | None
    pr_url: str | None

    @property
    def change_id(self) -> str:
        """The submitted change's change ID."""

        return self.prepared.change.change_id


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Remote branch and pull request state for the selected stack."""

    client: JjClient
    dry_run: bool
    changes: tuple[SubmittedChange, ...]
    trunk: LocalCommit


@dataclass(frozen=True, slots=True)
class GeneratedDescription:
    """Generated title/body pair for a pull request or stack summary."""

    body: str
    title: str


class PRMetadataAction(NamedTuple):
    """One planned additive metadata write for a pull request."""

    labels: list[str]
    reviewers: list[str]
    team_reviewers: list[str]


@dataclass(frozen=True, slots=True)
class PRSyncPlan:
    """Complete desired state for one pull request."""

    base_branch: str
    discovered_pr: GithubPR | None
    draft: bool
    generated_description: GeneratedDescription
    metadata: PRMetadataAction | None
    prepared: PreparedSubmitChange

    @property
    def action(self) -> PRAction:
        if self.discovered_pr is None:
            return "created"
        if any(update is not None for update in self.content_updates) or self.draft_action:
            return "updated"
        return "unchanged"

    @property
    def content_updates(self) -> tuple[str | None, str | None, str | None]:
        pr = self.discovered_pr
        if pr is None:
            return None, None, None
        return (
            self.base_branch if pr.base.ref != self.base_branch else None,
            (
                self.generated_description.body
                if (pr.body or "") != self.generated_description.body
                else None
            ),
            (
                self.generated_description.title
                if pr.title != self.generated_description.title
                else None
            ),
        )

    @property
    def draft_action(self) -> PRDraftAction | None:
        pr = self.discovered_pr
        if pr is None or pr.state != "open":
            return None
        if pr.is_draft == self.draft:
            return None
        return "draft" if self.draft else "ready"


@dataclass(frozen=True, slots=True)
class PreparedSubmitInputs:
    """Local submit inputs prepared before GitHub mutations begin."""

    branch_resolutions: tuple[ResolvedPRBranch, ...]
    client: JjClient
    generated_pr_descriptions: dict[str, GeneratedDescription]
    generated_stack_description: GeneratedDescription | None
    is_maximal_path: bool
    remote: GitRemote
    stack: LocalStack
    state: TrackingState


@dataclass(slots=True)
class SubmitMutationRun:
    """Mutable submit state shared by mutation phases."""

    dry_run: bool
    state: TrackingState
    state_store: TrackingStore

    def record_submission(
        self,
        *,
        baseline: SubmittedBaseline,
        change_id: str,
        identity: PRIdentity,
    ) -> None:
        """Save one GitHub-acknowledged PR snapshot."""

        if self.dry_run:
            return
        current = self.state.tracked_pr(change_id)
        if current is None:
            self.state = self.state_store.create_pr(
                change_id,
                identity=identity,
                baseline=baseline,
            )
            return
        if identity != current.pr_identity:
            raise RuntimeError(f"PR identity changed during submit for {change_id}.")
        self.state = self.state_store.relink_pr(
            change_id,
            identity=identity,
            baseline=baseline,
        )


class PrivateCommitFinder(Protocol):
    """Subset of the jj client interface needed for git.private-commits checks."""

    def find_private_commits(
        self,
        changes: tuple[LocalCommit, ...],
    ) -> tuple[LocalCommit, ...]:
        """Return the changes blocked by the repo's private-commit policy."""
