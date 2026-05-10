"""Data models for per-operation intent files."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class IntentBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1

    def change_ids(self) -> frozenset[str]:
        return frozenset()


class OperationIntent(IntentBase):
    pid: int
    label: str
    started_at: str  # ISO 8601


class OrderedChangeIdsIntent(OperationIntent):
    display_revset: str
    ordered_change_ids: tuple[str, ...]

    def change_ids(self) -> frozenset[str]:
        return frozenset(self.ordered_change_ids)


class SubmitIntent(OrderedChangeIdsIntent):
    kind: Literal["submit"]
    ordered_commit_ids: tuple[str, ...] = ()
    remote_name: str
    github_host: str
    github_owner: str
    github_repo: str
    bookmarks: dict[str, str]  # change_id → bookmark


class CleanupIntent(OperationIntent):
    kind: Literal["cleanup"]


class CleanupRebaseIntent(OrderedChangeIdsIntent):
    kind: Literal["cleanup-rebase"]
    ordered_commit_ids: tuple[str, ...] = ()


class CloseIntent(OrderedChangeIdsIntent):
    kind: Literal["close"]
    ordered_commit_ids: tuple[str, ...] = ()
    cleanup: bool


class RelinkIntent(OperationIntent):
    kind: Literal["relink"]
    change_id: str

    def change_ids(self) -> frozenset[str]:
        return frozenset([self.change_id])


type IntentFile = Annotated[
    SubmitIntent
    | CleanupIntent
    | CleanupRebaseIntent
    | CloseIntent
    | RelinkIntent,
    Field(discriminator="kind"),
]


class LoadedIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    intent: IntentFile


MatchResult = Literal["exact", "superset", "overlap", "disjoint"]
