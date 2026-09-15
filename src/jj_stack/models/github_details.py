"""Live review and check evidence for detailed inspection, never saved in tracking."""

from __future__ import annotations

from pydantic import AliasPath, BaseModel, Field, model_validator


class GithubReviewThread(BaseModel):
    is_resolved: bool = Field(alias="isResolved", exclude=True)
    is_outdated: bool = Field(alias="isOutdated")
    path: str
    line: int | None = None
    body: str = Field(default="", validation_alias=AliasPath("comments", "nodes", 0, "bodyText"))
    url: str | None = Field(
        default=None, validation_alias=AliasPath("comments", "nodes", 0, "url")
    )


class GithubCheck(BaseModel):
    name: str
    state: str
    url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_check_run(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            **value,
            "state": value.get("conclusion") or value.get("status") or value.get("state"),
        }


class GithubPRMergeDetails(BaseModel):
    unresolved_threads: tuple[GithubReviewThread, ...] = ()
    checks: tuple[GithubCheck, ...] = ()
