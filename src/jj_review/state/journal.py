"""Append-only operation journal storage."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from jj_review.state.operation_lock import OperationLockHolder

JournalEventKind = Literal[
    "begin",
    "planned_mutation",
    "mutation_applied",
    "saved_state_update",
    "completed",
    "abandoned",
]

JOURNAL_DIRNAME = "journals"


@dataclass(frozen=True, slots=True)
class JournalEvent:
    """One append-only operation journal event."""

    event: JournalEventKind
    operation: str
    operation_id: str
    timestamp: str
    data: dict[str, Any]


class OperationJournal:
    """Append-only JSONL journal for one operation."""

    def __init__(
        self,
        *,
        operation: str,
        operation_id: str,
        path: Path,
    ) -> None:
        self.operation = operation
        self.operation_id = operation_id
        self.path = path

    @classmethod
    def begin(
        cls,
        state_dir: Path,
        *,
        operation: str,
        options: dict[str, Any],
        resolved_scope: dict[str, Any],
        lock_holder: OperationLockHolder | None,
    ) -> OperationJournal:
        """Create a new operation journal and append its begin event."""

        operation_id = uuid4().hex
        path = _journal_path(state_dir, operation=operation, operation_id=operation_id)
        journal = cls(operation=operation, operation_id=operation_id, path=path)
        journal.append(
            "begin",
            {
                "lock_holder": None if lock_holder is None else asdict(lock_holder),
                "options": options,
                "resolved_scope": resolved_scope,
            },
        )
        return journal

    @classmethod
    def open(cls, path: Path) -> OperationJournal:
        """Open an existing journal for appending."""

        events = read_journal(path)
        if not events:
            raise ValueError(f"Journal is empty: {path}")
        first = events[0]
        return cls(
            operation=first.operation,
            operation_id=first.operation_id,
            path=path,
        )

    def append(self, event: JournalEventKind, data: dict[str, Any]) -> None:
        """Append one event to the journal."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = JournalEvent(
            event=event,
            operation=self.operation,
            operation_id=self.operation_id,
            timestamp=datetime.now(UTC).isoformat(),
            data=data,
        )
        with self.path.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(_jsonable(asdict(entry)), sort_keys=True))
            journal.write("\n")


def read_journal(path: Path) -> tuple[JournalEvent, ...]:
    """Read a JSONL operation journal."""

    events: list[JournalEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        raw = json.loads(line)
        events.append(
            JournalEvent(
                data=dict(raw["data"]),
                event=raw["event"],
                operation=str(raw["operation"]),
                operation_id=str(raw["operation_id"]),
                timestamp=str(raw["timestamp"]),
            )
        )
    return tuple(events)


def _journal_path(state_dir: Path, *, operation: str, operation_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d-%H-%M-%S")
    filename = f"{timestamp}-{operation}-{operation_id}.jsonl"
    return state_dir / JOURNAL_DIRNAME / filename


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json", exclude_none=True))
    return value
