"""Append-only operation audit log storage."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from jj_review.state.operation_lock import read_operation_lock_holder

JournalEventKind = Literal[
    "begin",
    "planned_mutation",
    "mutation_applied",
    "saved_state_update",
    "completed",
]

OPERATION_LOG_FILENAME = "operation-log.jsonl"


@dataclass(frozen=True, slots=True)
class JournalEvent:
    """One append-only operation log event."""

    event: JournalEventKind
    operation: str
    operation_id: str
    timestamp: str
    data: dict[str, Any]


class OperationJournal:
    """Repo-level audit log handle for one mutating operation."""

    def __init__(
        self,
        *,
        operation: str,
        operation_id: str,
        state_dir: Path,
    ) -> None:
        self.operation = operation
        self.operation_id = operation_id
        self.path = state_dir / OPERATION_LOG_FILENAME

    @classmethod
    def begin(
        cls,
        state_dir: Path,
        *,
        durable: bool = False,
        operation: str,
        options: dict[str, Any],
        resolved_scope: dict[str, Any],
    ) -> OperationJournal:
        """Append a begin event and return a handle for later events."""

        journal = cls(
            operation=operation,
            operation_id=uuid4().hex,
            state_dir=state_dir,
        )
        lock_holder = read_operation_lock_holder(state_dir)
        journal.append(
            "begin",
            {
                "lock_holder": None if lock_holder is None else asdict(lock_holder),
                "options": options,
                "resolved_scope": resolved_scope,
            },
            durable=durable,
        )
        return journal

    def append(
        self,
        event: JournalEventKind,
        data: dict[str, Any],
        *,
        durable: bool = False,
    ) -> None:
        """Append one event to the repo operation log."""

        _append_event(
            self.path,
            JournalEvent(
                event=event,
                operation=self.operation,
                operation_id=self.operation_id,
                timestamp=datetime.now(UTC).isoformat(),
                data=data,
            ),
            durable=durable,
        )


def read_operation_log(state_dir: Path) -> tuple[JournalEvent, ...]:
    """Read the repo-level operation audit log."""

    path = state_dir / OPERATION_LOG_FILENAME
    if not path.exists():
        return ()
    return _read_events(path)


def _read_events(path: Path) -> tuple[JournalEvent, ...]:
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


def _append_event(path: Path, entry: JournalEvent, *, durable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_jsonable(asdict(entry)), sort_keys=True) + "\n"
    created_log = not path.exists()
    with path.open("a", encoding="utf-8") as output:
        output.write(payload)
        if durable:
            output.flush()
            os.fsync(output.fileno())
    if durable and created_log:
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
