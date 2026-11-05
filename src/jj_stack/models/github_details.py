"""Live review and check evidence for detailed inspection, never saved in tracking."""

from __future__ import annotations

from pydantic import AliasPath, BaseModel, Field, field_validator, model_validator


class GithubCheckStateCount(BaseModel):
    state: str
    count: int


class GithubCheckCounts(BaseModel):
    total: int = Field(alias="totalCount")
    runs: tuple[GithubCheckStateCount, ...] | None = Field(alias="checkRunCountsByState")
    statuses: tuple[GithubCheckStateCount, ...] | None = Field(alias="statusContextCountsByState")

    @property
    def remaining(self) -> int | None:
        return self._count({"EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING"})

    @property
    def failed(self) -> int | None:
        return self._count(
            {
                "FAILURE",
                "ERROR",
                "ACTION_REQUIRED",
                "CANCELLED",
                "TIMED_OUT",
                "STALE",
                "STARTUP_FAILURE",
            }
        )

    def _count(self, states: set[str]) -> int | None:
        if self.runs is None or self.statuses is None:
            return None
        return sum(item.count for item in (*self.runs, *self.statuses) if item.state in states)


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
    is_required: bool = Field(default=False, alias="isRequired", exclude=True)
    run_id: int | None = Field(default=None, alias="databaseId", exclude=True)
    app_id: int | None = Field(
        default=None, validation_alias=AliasPath("checkSuite", "app", "databaseId"), exclude=True
    )
    workflow_id: int | None = Field(
        default=None,
        validation_alias=AliasPath("checkSuite", "workflowRun", "workflow", "databaseId"),
        exclude=True,
    )

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

    @field_validator("checks", "merge_checks")
    @classmethod
    def _latest_check_runs(cls, checks: tuple[GithubCheck, ...]) -> tuple[GithubCheck, ...]:
        # Rollups include superseded runs. Check-run IDs distinguish replacements even
        # before a queued job starts; start/completion times can put older runs last.
        # Keep distinct apps/workflows and commit statuses independently required.
        latest: dict[tuple[str, int | None, int | None], GithubCheck] = {}
        for check in checks:
            if check.run_id is not None:
                key = (check.name, check.app_id, check.workflow_id)
                prior = latest.get(key)
                if prior is None or check.run_id > (prior.run_id or 0):
                    latest[key] = check
        return tuple(
            check
            for check in checks
            if check.run_id is None
            or latest[(check.name, check.app_id, check.workflow_id)] is check
        )

    @property
    def missing_checks(self) -> tuple[str, ...]:
        reported = {check.name for check in (*self.checks, *self.merge_checks)}
        return tuple(name for name in self.required_checks if name not in reported)
