"""GitHub API response models."""

from collections.abc import Mapping
from typing import Self

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


class GithubPullRequest(BaseModel):
    """Subset of pull request fields used by the client."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    base: GithubBranchRef
    body: str | None = None
    head: GithubBranchRef
    html_url: str
    is_draft: bool = Field(default=False, alias="draft")
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

        head_ref = value.get("headRefName")
        payload: dict[str, object] = {
            "base": {"ref": value.get("baseRefName")},
            "body": value.get("body"),
            "head": {
                "label": _graphql_head_label(value),
                "ref": head_ref,
            },
            "html_url": value.get("url"),
            "merged_at": value.get("mergedAt"),
            "number": value.get("number"),
            "state": value.get("state", ""),
            "title": value.get("title"),
        }
        if isinstance(payload["state"], str):
            payload["state"] = payload["state"].lower()
        if "isDraft" in value:
            payload["draft"] = value.get("isDraft")
        if "id" in value:
            payload["node_id"] = value.get("id")
        if "reviewDecision" in value:
            payload["review_decision"] = _normalize_graphql_review_decision(
                value.get("reviewDecision")
            )
        return payload


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

