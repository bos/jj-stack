"""Append-only operation audit log storage."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from jj_stack.state.operation_lock import read_operation_lock_holder

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

    @classmethod
    def resume(
        cls,
        state_dir: Path,
        *,
        operation: str,
        operation_id: str,
    ) -> OperationJournal:
        """Return a handle that appends to an existing operation's audit stream."""

        return cls(
            operation=operation,
            operation_id=operation_id,
            path=state_dir / OPERATION_LOG_FILENAME,
        )

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
        _terminate_partial_line(self.path)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(entry.model_dump_json(exclude_none=True) + "\n")
            if durable:
                output.flush()
                os.fsync(output.fileno())
        if durable and created_log:
            try:
                fd = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @contextmanager
    def mutation(self, mutation: str, /, **data: Any) -> Iterator[None]:
        payload = {"mutation": mutation, **data}
        self.append("planned_mutation", payload)
        yield
        self.append("mutation_applied", payload)

    def record_saved_state_updates(
        self,
        *,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> None:
        """Emit one ``saved_state_update`` event per change whose record changed."""

        for change_id in sorted({*before, *after}):
            before_change = before.get(change_id)
            after_change = after.get(change_id)
            if before_change == after_change:
                continue
            self.append(
                "saved_state_update",
                {"after": after_change, "before": before_change, "change_id": change_id},
            )


def read_operation_log(state_dir: Path) -> tuple[JournalEvent, ...]:
    """Read valid audit events, ignoring malformed best-effort records."""

    path = state_dir / OPERATION_LOG_FILENAME
    if not path.exists():
        return ()
    lines = tuple(
        line for line in path.read_text(encoding="utf-8").splitlines() if line
    )
    events: list[JournalEvent] = []
    for line in lines:
        try:
            events.append(JournalEvent.model_validate_json(line))
        except ValidationError:
            continue
    return tuple(events)


def _terminate_partial_line(path: Path) -> None:
    """Keep a torn append from absorbing the next valid audit record."""

    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb+") as output:
        output.seek(-1, os.SEEK_END)
        if output.read(1) == b"\n":
            return
        output.seek(0, os.SEEK_END)
        output.write(b"\n")
