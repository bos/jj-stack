"""GitHub API response models."""

from typing import Literal, get_args

from pydantic import (
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from jj_stack.identifiers import CommitId
from jj_stack.models.github_details import GithubMergeQueueEntry, GithubPRMergeDetails

CheckRollupStatus = Literal["failed", "passed", "pending"]
PRState = Literal["open", "closed", "merged"]
ReviewDecision = Literal["approved", "changes_requested", "review_required"]
DEFAULT_PR_TEMPLATE_PATHS = tuple(
    f"{directory}{name}"
    for directory in (".github/", "", "docs/")
    for name in ("PULL_REQUEST_TEMPLATE.md", "pull_request_template.md")
)

_CHECK_ROLLUP_STATUSES: dict[str, CheckRollupStatus] = {
    "ERROR": "failed",
    "EXPECTED": "pending",
    "FAILURE": "failed",
    "PENDING": "pending",
    "SUCCESS": "passed",
}


class GithubRepoPermissions(BaseModel):
    """The token's permissions on the repo; GitHub reports them only to authenticated requests."""

    push: bool


class GithubRepo(BaseModel):
    """Subset of repo fields used by the client."""

    allow_merge_commit: bool | None = None
    allow_rebase_merge: bool | None = None
    allow_squash_merge: bool | None = None
    default_branch: str | None
    full_name: str
    node_id: str
    permissions: GithubRepoPermissions | None = None


class GithubBranchRef(BaseModel):
    """Subset of branch-ref fields embedded in pull request payloads."""

    ref: str


class GithubPRHead(BaseModel):
    """PR head branch and commit, with the owner label when available."""

    label: str | None = None
    ref: str
    sha: CommitId


class GithubStackPR(BaseModel):
    """Pull request state embedded in a GitHub stack response."""

    head: GithubPRHead
    number: int
    merged_at: str | None = None

    @property
    def is_historical(self) -> bool:
        return self.merged_at is not None


class GithubStack(BaseModel):
    """Ordered pull requests in one GitHub stack."""

    number: int
    prs: tuple[GithubStackPR, ...] = Field(alias="pull_requests", min_length=1)

    @property
    def pr_numbers(self) -> tuple[int, ...]:
        return tuple(pr.number for pr in self.prs)

    @property
    def historical_prs(self) -> tuple[GithubStackPR, ...]:
        return tuple(pr for pr in self.prs if pr.is_historical)

    @property
    def active_pr_numbers(self) -> tuple[int, ...]:
        return tuple(pr.number for pr in self.prs if not pr.is_historical)

    @property
    def has_merged_prefix(self) -> bool:
        """Whether every merged member sits below every active one."""

        return self.prs[: len(self.historical_prs)] == self.historical_prs


class GithubStackMergeDetails(BaseModel):
    """Details returned by GitHub's asynchronous stack merge endpoint."""

    expected_head_sha: CommitId | None = None
    merge_action: str | None = None
    merge_method: str | None = None
    message: str | None = None
    sha: CommitId | None = None
    uuid: str | None = None


class GithubStackMerge(BaseModel):
    """Pending or terminal asynchronous stack merge state."""

    details: GithubStackMergeDetails
    status: Literal["enqueued", "failed", "merged", "pending"]


class GithubStackMergeSubmission(BaseModel):
    """Typed submit response for an asynchronous merge request.

    `already_pending` reports GitHub's 409, which means an operation for this pull request is
    already in flight. It does not mean the merge conflicts; a conflict comes back later as a
    failed terminal status.
    """

    already_pending: bool
    result: GithubStackMerge


class GithubPR(BaseModel):
    """Pull request fields read from GitHub's GraphQL API."""

    model_config = ConfigDict(populate_by_name=True)

    base: GithubBranchRef
    body: str | None = None
    check_rollup_status: CheckRollupStatus | None = Field(
        default=None, validation_alias=AliasPath("statusCheckRollup", "state")
    )
    head: GithubPRHead
    # GitHub reports a null `headRef` once the head branch is deleted.
    head_branch_exists: bool = Field(default=True, validation_alias="headRef")
    html_url: str = Field(validation_alias="url")
    is_draft: bool = Field(default=False, validation_alias="isDraft")
    merge_queue_entry: GithubMergeQueueEntry | None = Field(
        default=None, validation_alias="mergeQueueEntry"
    )
    queue_removal_reason: str | None = Field(
        default=None, validation_alias=AliasPath("timelineItems", "nodes", 0, "reason")
    )
    queue_test_commit: CommitId | None = Field(
        default=None,
        validation_alias=AliasPath("timelineItems", "nodes", 0, "beforeCommit", "oid"),
    )
    merge_commit_sha: CommitId | None = Field(
        default=None, validation_alias=AliasPath("mergeCommit", "oid")
    )
    merge_state_status: str | None = Field(default=None, validation_alias="mergeStateStatus")
    merge_details: GithubPRMergeDetails | None = None
    # Approvals from reviewers with write access, when looked up.
    approvals: int | None = None
    # How many commits the landing branch has that this PR's head lacks, when looked up.
    behind: int | None = None
    node_id: str = Field(validation_alias="id")
    number: int
    review_decision: ReviewDecision | None = Field(
        default=None, validation_alias="reviewDecision"
    )
    # The branch the PR's GitHub stack lands on, when the PR is in one.
    stack_base_ref: str | None = Field(
        default=None, validation_alias=AliasPath("stack", "baseRefName")
    )
    state: PRState
    title: str

    @property
    def is_queued(self) -> bool:
        return self.merge_queue_entry is not None

    @model_validator(mode="before")
    @classmethod
    def _nest_graphql_refs(cls, value: object) -> object:
        # GraphQL reports the branch names and head commit as top-level fields; a model built
        # by field name already has them nested.
        if not isinstance(value, dict) or "baseRefName" not in value:
            return value
        head = _GraphqlHead.model_validate(value)
        label = None if head.owner is None else f"{head.owner.login}:{head.ref}"
        return {
            **value,
            "base": {"ref": value["baseRefName"]},
            "head": {"label": label, "ref": head.ref, "sha": head.sha},
        }

    @field_validator("check_rollup_status", mode="before")
    @classmethod
    def _normalize_check_rollup(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        status = _CHECK_ROLLUP_STATUSES.get(value, value)
        # GitHub may add states; an unknown one reads as no status rather than a failure.
        return status if status in get_args(CheckRollupStatus) else None

    @field_validator("head_branch_exists", mode="before")
    @classmethod
    def _head_ref_exists(cls, value: object) -> object:
        return value if isinstance(value, bool) else value is not None

    @field_validator("review_decision", mode="before")
    @classmethod
    def _normalize_review_decision(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        decision = value.lower()
        return decision if decision in get_args(ReviewDecision) else None

    @field_validator("state", mode="before")
    @classmethod
    def _lowercase_state(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value


class _GraphqlHeadOwner(BaseModel):
    login: str


class _GraphqlHead(BaseModel):
    ref: str = Field(alias="headRefName")
    sha: CommitId = Field(alias="headRefOid")
    owner: _GraphqlHeadOwner | None = Field(default=None, alias="headRepositoryOwner")


class GithubPRReviewUser(BaseModel):
    """Subset of review-author fields used to summarize PR reviews."""

    login: str


class GithubPRReview(BaseModel):
    """Subset of PR review fields used by the client."""

    id: int
    state: str
    user: GithubPRReviewUser | None = None


class GithubIssueComment(BaseModel):
    """Subset of issue-comment fields used by the client."""

    body: str
    id: int = Field(alias="databaseId")


class GithubPRRevision(BaseModel):
    """One available pull request revision observed from a force push."""

    before_commit_id: CommitId
    commit_id: CommitId
    is_current: bool
    version: int
