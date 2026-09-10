"""GitHub API response models."""

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jj_stack.identifiers import CommitId

CheckRollupStatus = Literal["failed", "passed", "pending"]
PRState = Literal["open", "closed", "merged"]


class GithubRepoPermissions(BaseModel):
    """The token's permissions on the repo; GitHub reports them only to authenticated requests."""

    model_config = ConfigDict(extra="ignore")

    push: bool


class GithubRepo(BaseModel):
    """Subset of repo fields used by the client."""

    model_config = ConfigDict(extra="ignore")

    allow_merge_commit: bool | None = None
    allow_rebase_merge: bool | None = None
    allow_squash_merge: bool | None = None
    default_branch: str | None
    full_name: str
    permissions: GithubRepoPermissions | None = None


class GithubBranchRef(BaseModel):
    """Subset of branch-ref fields embedded in pull request payloads."""

    model_config = ConfigDict(extra="ignore")

    ref: str


class GithubPRHead(BaseModel):
    """PR head branch and commit, with the owner label when available."""

    model_config = ConfigDict(extra="ignore")

    label: str | None = None
    ref: str
    sha: CommitId


class GithubStackPR(BaseModel):
    """Pull request state embedded in a GitHub stack response."""

    model_config = ConfigDict(extra="ignore")

    head: GithubPRHead
    number: int
    merged_at: str | None = None

    @property
    def is_historical(self) -> bool:
        return self.merged_at is not None


class GithubStack(BaseModel):
    """Ordered pull requests in one GitHub stack."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

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

    model_config = ConfigDict(extra="ignore")

    expected_head_sha: CommitId | None = None
    merge_action: str | None = None
    merge_method: str | None = None
    message: str | None = None
    sha: CommitId | None = None
    uuid: str | None = None


class GithubStackMerge(BaseModel):
    """Pending or terminal asynchronous stack merge state."""

    model_config = ConfigDict(extra="ignore")

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
    """Pull request fields with one lifecycle across REST and GraphQL responses."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    base: GithubBranchRef
    body: str | None = None
    check_rollup_status: CheckRollupStatus | None = None
    head: GithubPRHead
    # GitHub reports a null `headRef` once the head branch is deleted; REST payloads say
    # nothing, so they keep the safe default.
    head_branch_exists: bool = True
    html_url: str
    is_draft: bool = Field(default=False, alias="draft")
    is_queued: bool = False
    merge_commit_sha: CommitId | None = None
    merged_at: str | None = None
    node_id: str
    number: int
    review_decision: str | None = None
    state: PRState
    title: str

    @model_validator(mode="after")
    def _normalize_merged_state(self) -> Self:
        if self.state == "closed" and self.merged_at is not None:
            self.state = "merged"
        return self

    @model_validator(mode="before")
    @classmethod
    def _normalize_graphql_payload(cls, value: object) -> object:
        if not isinstance(value, dict) or "baseRefName" not in value:
            return value

        payload: dict[str, object] = {
            "base": {"ref": value.get("baseRefName")},
            "body": value.get("body"),
            "check_rollup_status": _normalize_graphql_check_rollup(
                value.get("statusCheckRollup")
            ),
            "draft": value.get("isDraft", False),
            "head": {
                "label": _graphql_head_label(value),
                "ref": value.get("headRefName"),
                "sha": value.get("headRefOid"),
            },
            "head_branch_exists": value.get("headRef", True) is not None,
            "html_url": value.get("url"),
            "is_queued": value.get("mergeQueueEntry") is not None,
            "merge_commit_sha": _graphql_merge_commit_oid(value.get("mergeCommit")),
            "merged_at": value.get("mergedAt"),
            "node_id": value.get("id"),
            "number": value.get("number"),
            "review_decision": _normalize_graphql_review_decision(value.get("reviewDecision")),
            "state": value.get("state", ""),
            "title": value.get("title"),
        }
        if isinstance(payload["state"], str):
            payload["state"] = payload["state"].lower()
        return payload


def _graphql_merge_commit_oid(value: object) -> str | None:
    oid = value.get("oid") if isinstance(value, dict) else None
    return oid if isinstance(oid, str) else None


class GithubPRReviewUser(BaseModel):
    """Subset of review-author fields used to summarize PR reviews."""

    model_config = ConfigDict(extra="ignore")

    login: str


class GithubPRReview(BaseModel):
    """Subset of PR review fields used by the client."""

    model_config = ConfigDict(extra="ignore")

    id: int
    state: str
    user: GithubPRReviewUser | None = None


class GithubIssueComment(BaseModel):
    """Subset of issue-comment fields used by the client."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    body: str
    id: int = Field(alias="databaseId")


class GithubPRRevision(BaseModel):
    """One available pull request revision observed from a force push."""

    before_commit_id: CommitId
    commit_id: CommitId
    is_current: bool
    version: int


def _graphql_head_label(raw_pr: Mapping[str, object]) -> str | None:
    try:
        parts = _GraphqlHeadLabelParts.model_validate(raw_pr)
    except ValidationError as error:
        raise ValueError("GitHub pull request GraphQL response had invalid head data.") from error
    if parts.head_repo_owner is None:
        return None
    return f"{parts.head_repo_owner.login}:{parts.head_ref_name}"


class _GraphqlHeadRepoOwner(BaseModel):
    login: str


class _GraphqlHeadLabelParts(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    head_ref_name: str = Field(alias="headRefName")
    head_repo_owner: _GraphqlHeadRepoOwner | None = Field(
        default=None,
        alias="headRepositoryOwner",
    )


def _normalize_graphql_review_decision(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.upper()
    if normalized == "APPROVED":
        return "approved"
    if normalized == "CHANGES_REQUESTED":
        return "changes_requested"
    return None


class _GraphqlCheckRollup(BaseModel):
    state: str


def _normalize_graphql_check_rollup(value: object) -> CheckRollupStatus | None:
    if value is None:
        return None
    try:
        rollup = _GraphqlCheckRollup.model_validate(value)
    except ValidationError as error:
        message = "GitHub pull request GraphQL response had invalid check data."
        raise ValueError(message) from error
    normalized = rollup.state.upper()
    if normalized == "SUCCESS":
        return "passed"
    if normalized in {"ERROR", "FAILURE"}:
        return "failed"
    if normalized in {"EXPECTED", "PENDING"}:
        return "pending"
    return None
