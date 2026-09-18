"""Live review and check evidence for detailed inspection, never saved in tracking."""

from __future__ import annotations

from pydantic import AliasPath, BaseModel, Field, model_validator


class GithubCheckStateCount(BaseModel):
    state: str
    count: int


class GithubCheckCounts(BaseModel):
    total: int = Field(alias="totalCount")
    runs: tuple[GithubCheckStateCount, ...] | None = Field(alias="checkRunCountsByState")
    statuses: tuple[GithubCheckStateCount, ...] | None = Field(alias="statusContextCountsByState")

    @property
    def remaining(self) -> int | None:
        if self.runs is None or self.statuses is None:
            return None
        return sum(
            item.count
            for item in (*self.runs, *self.statuses)
            if item.state in {"EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING"}
        )

    @property
    def failed(self) -> int:
        return sum(
            item.count
            for item in (*(self.runs or ()), *(self.statuses or ()))
            if item.state
            in {
                "FAILURE",
                "ERROR",
                "ACTION_REQUIRED",
                "CANCELLED",
                "TIMED_OUT",
                "STALE",
                "STARTUP_FAILURE",
            }
        )


class GithubMergeQueueEntry(BaseModel):
    position: int | None = None
    state: str | None = None
    estimated_seconds: int | None = Field(default=None, alias="estimatedTimeToMerge")
    total: int | None = Field(
        default=None, validation_alias=AliasPath("mergeQueue", "entries", "totalCount")
    )
    checks: GithubCheckCounts | None = Field(
        default=None,
        validation_alias=AliasPath("headCommit", "statusCheckRollup", "contexts"),
    )


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
    mergeable: str | None = None
    required_checks: tuple[str, ...] = ()
    resolve_threads: bool = False
    unresolved_threads: tuple[GithubReviewThread, ...] = ()
    checks: tuple[GithubCheck, ...] = ()
    merge_checks: tuple[GithubCheck, ...] = ()

    @property
    def missing_checks(self) -> tuple[str, ...]:
        reported = {check.name for check in (*self.checks, *self.merge_checks)}
        return tuple(name for name in self.required_checks if name not in reported)
