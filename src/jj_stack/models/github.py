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


class GithubStackPullRequest(BaseModel):
    """Pull request identity embedded in a native stack response."""

    model_config = ConfigDict(extra="ignore")

    number: int


class GithubStack(BaseModel):
    """Ordered pull requests in one native GitHub stack."""

    model_config = ConfigDict(extra="ignore")

    number: int
    pull_requests: tuple[GithubStackPullRequest, ...]

    @property
    def pull_request_numbers(self) -> tuple[int, ...]:
        return tuple(pull_request.number for pull_request in self.pull_requests)


class GithubBranchRef(BaseModel):
    """Subset of branch-ref fields embedded in pull request payloads."""

    model_config = ConfigDict(extra="ignore")

    label: str | None = None
    ref: str
    sha: str | None = None


class GithubPullRequest(BaseModel):
    """Subset of pull request fields used by the client."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    base: GithubBranchRef
    body: str | None = None
    head: GithubBranchRef
    html_url: str
    is_draft: bool = Field(default=False, alias="draft")
    landing_owners: frozenset[Literal["auto_merge", "merge_queue"]] | None = None
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
            "landing_owners": _graphql_landing_owners(value),
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


def _graphql_landing_owners(
    value: Mapping[str, object],
) -> frozenset[Literal["auto_merge", "merge_queue"]] | None:
    fields = (("autoMergeRequest", "auto_merge"), ("mergeQueueEntry", "merge_queue"))
    if not all(field in value for field, _owner in fields):
        return None
    return frozenset(owner for field, owner in fields if value[field] is not None)


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
