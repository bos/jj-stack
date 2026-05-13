"""Append-only operation audit log storage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from jj_review.state.operation_lock import read_operation_lock_holder

JournalEventKind = Literal[
    "begin",
    "planned_mutation",
    "mutation_applied",
    "saved_state_update",
    "completed",
]

OPERATION_LOG_FILENAME = "operation-log.jsonl"


class JournalEvent(BaseModel):
    """One append-only operation log event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: JournalEventKind
    operation: str
    operation_id: str
    timestamp: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OperationJournal:
    """Repo-level audit log handle for one mutating operation.

    Dry-run and blocked paths use :meth:`disabled` so mutation code can keep a
    non-optional journal and record the same events unconditionally.
    """

    operation: str
    operation_id: str
    path: Path | None

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
            path=state_dir / OPERATION_LOG_FILENAME,
        )
        lock_holder = read_operation_lock_holder(state_dir)
        journal.append(
            "begin",
            {
                "lock_holder": lock_holder,
                "options": options,
                "resolved_scope": resolved_scope,
            },
            durable=durable,
        )
        return journal

    @classmethod
    def disabled(cls) -> OperationJournal:
        """Return a no-op journal for paths that should not write audit events."""

        return cls(operation="", operation_id="", path=None)

    def append(
        self,
        event: JournalEventKind,
        data: dict[str, Any],
        *,
        durable: bool = False,
    ) -> None:
        """Append one event to the repo operation log."""

        if self.path is None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        created_log = not self.path.exists()
        entry = JournalEvent(
            event=event,
            operation=self.operation,
            operation_id=self.operation_id,
            timestamp=datetime.now(UTC).isoformat(),
            data=data,
        )
        with self.path.open("a", encoding="utf-8") as output:
            output.write(entry.model_dump_json(exclude_none=True) + "\n")
            if durable:
                output.flush()
                os.fsync(output.fileno())
        if durable and created_log:
            _fsync_directory(self.path.parent)


def read_operation_log(state_dir: Path) -> tuple[JournalEvent, ...]:
    """Read the repo-level operation audit log."""

    path = state_dir / OPERATION_LOG_FILENAME
    if not path.exists():
        return ()
    return _read_events(path)


def _read_events(path: Path) -> tuple[JournalEvent, ...]:
    return tuple(
        JournalEvent.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
