"""GitHub API response models."""

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class GithubRepository(BaseModel):
    """Subset of repository fields used by the client."""

    model_config = ConfigDict(extra="ignore")

    allow_merge_commit: bool | None = None
    allow_rebase_merge: bool | None = None
    allow_squash_merge: bool | None = None
    clone_url: str
    default_branch: str | None
    full_name: str
    html_url: str
    name: str
    private: bool
    url: str


class GithubBranchRef(BaseModel):
    """Subset of branch-ref fields embedded in pull request payloads."""

    model_config = ConfigDict(extra="ignore")

    label: str | None = None
    ref: str
    sha: str | None = None


class GithubStackPullRequestHead(BaseModel):
    """Exact reviewed branch head embedded in a native stack response."""

    model_config = ConfigDict(extra="ignore")

    ref: str
    sha: str


class GithubStackPullRequest(BaseModel):
    """Pull request state embedded in a native stack response."""

    model_config = ConfigDict(extra="ignore")

    head: GithubStackPullRequestHead
    merged_at: str | None
    number: int
    state: str

    @property
    def is_historical(self) -> bool:
        return self.merged_at is not None


class GithubStack(BaseModel):
    """Ordered pull requests in one native GitHub stack."""

    model_config = ConfigDict(extra="ignore")

    number: int
    pull_requests: tuple[GithubStackPullRequest, ...]

    @property
    def pull_request_numbers(self) -> tuple[int, ...]:
        return tuple(pull_request.number for pull_request in self.pull_requests)

    @property
    def historical_pull_requests(self) -> tuple[GithubStackPullRequest, ...]:
        return tuple(
            pull_request for pull_request in self.pull_requests if pull_request.is_historical
        )

    @property
    def historical_pull_request_numbers(self) -> tuple[int, ...]:
        return tuple(pull_request.number for pull_request in self.historical_pull_requests)

    @property
    def active_pull_requests(self) -> tuple[GithubStackPullRequest, ...]:
        return tuple(
            pull_request for pull_request in self.pull_requests if not pull_request.is_historical
        )

    @property
    def active_pull_request_numbers(self) -> tuple[int, ...]:
        return tuple(pull_request.number for pull_request in self.active_pull_requests)

    @model_validator(mode="after")
    def _validate_historical_prefix(self) -> Self:
        active_seen = False
        for pull_request in self.pull_requests:
            if not pull_request.is_historical:
                active_seen = True
            elif active_seen:
                raise ValueError("Merged native stack members must form a bottom prefix.")
        return self


class GithubAsyncMergeDetails(BaseModel):
    """Details returned by GitHub's native asynchronous merge endpoint."""

    model_config = ConfigDict(extra="ignore")

    expected_head_sha: str | None = None
    merge_method: str | None = None
    message: str | None = None
    sha: str | None = None
    uuid: str | None = None


class GithubAsyncMerge(BaseModel):
    """Pending or terminal native asynchronous merge state."""

    model_config = ConfigDict(extra="ignore")

    details: GithubAsyncMergeDetails
    status: Literal["failed", "merged", "pending"]


class GithubAsyncMergeSubmission(BaseModel):
    """Typed submit response, including a decoded conflict diagnostic."""

    conflict: bool
    result: GithubAsyncMerge


class GithubPullRequest(BaseModel):
    """Subset of pull request fields used by the client."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    base: GithubBranchRef
    body: str | None = None
    head: GithubBranchRef
    html_url: str
    is_draft: bool = Field(default=False, alias="draft")
    merge_commit_sha: str | None = None
    merged_at: str | None = None
    node_id: str | None = None
    number: int
    review_decision: str | None = None
    state: str
    title: str

    def normalize_state(self) -> Self:
        if self.state != "closed" or self.merged_at is None:
            return self
        return self.model_copy(update={"state": "merged"})

    @model_validator(mode="before")
    @classmethod
    def _normalize_graphql_payload(cls, value: object) -> object:
        if not isinstance(value, dict) or "baseRefName" not in value:
            return value

        payload: dict[str, object] = {
            "base": {"ref": value.get("baseRefName")},
            "body": value.get("body"),
            "draft": value.get("isDraft", False),
            "head": {
                "label": _graphql_head_label(value),
                "ref": value.get("headRefName"),
                "sha": value.get("headRefOid"),
            },
            "html_url": value.get("url"),
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


class GithubPullRequestReviewUser(BaseModel):
    """Subset of review-author fields used to summarize PR reviews."""

    model_config = ConfigDict(extra="ignore")

    login: str


class GithubPullRequestReview(BaseModel):
    """Subset of PR review fields used by the client."""

    model_config = ConfigDict(extra="ignore")

    id: int
    state: str
    user: GithubPullRequestReviewUser | None = None


class GithubIssueComment(BaseModel):
    """Subset of issue-comment fields used by the client."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    body: str
    html_url: str = Field(alias="url")
    id: int = Field(alias="databaseId")


def _graphql_head_label(raw_pull_request: Mapping[str, object]) -> str | None:
    try:
        parts = _GraphqlHeadLabelParts.model_validate(raw_pull_request)
    except ValidationError as error:
        raise ValueError("GitHub pull request GraphQL response had invalid head data.") from error
    if parts.head_repository_owner is None or parts.head_repository_owner.login is None:
        return None
    if parts.head_ref_name is None:
        return None
    return f"{parts.head_repository_owner.login}:{parts.head_ref_name}"


class _GraphqlHeadRepositoryOwner(BaseModel):
    login: str | None = None


class _GraphqlHeadLabelParts(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    head_ref_name: str | None = Field(default=None, alias="headRefName")
    head_repository_owner: _GraphqlHeadRepositoryOwner | None = Field(
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
