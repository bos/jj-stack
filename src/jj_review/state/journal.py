"""Append-only operation journal storage."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from jj_review.state.operation_lock import OperationLockHolder, read_operation_lock_holder

JournalEventKind = Literal[
    "begin",
    "planned_mutation",
    "mutation_applied",
    "saved_state_update",
    "completed",
    "abandoned",
]

JOURNAL_DIRNAME = "journals"
MIN_RETAINED_JOURNALS = 50
MIN_RETAINED_JOURNAL_AGE = timedelta(days=30)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JournalEvent:
    """One append-only operation journal event."""

    event: JournalEventKind
    operation: str
    operation_id: str
    timestamp: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LandOperationRecord:
    """Journal-backed recovery record for one incomplete `land` operation."""

    kind: Literal["land"]
    path: Path
    pid: int
    label: str
    started_at: str
    bypass_readiness: bool
    cleanup_bookmarks: bool
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    ordered_commit_ids: tuple[str, ...]
    landed_change_ids: tuple[str, ...]
    landed_bookmarks: dict[str, str]
    landed_bookmark_managed: dict[str, bool]
    landed_commit_ids: dict[str, str]
    landed_pull_request_numbers: dict[str, int]
    landed_subjects: dict[str, str]
    trunk_branch: str
    landed_commit_id: str
    selected_pr_number: int | None = None

    def change_ids(self) -> frozenset[str]:
        """Return the change IDs mentioned by this operation."""

        return frozenset(self.ordered_change_ids)


@dataclass(frozen=True, slots=True)
class SubmitOperationRecord:
    """Journal-backed recovery record for one incomplete `submit` operation."""

    kind: Literal["submit"]
    path: Path
    pid: int
    label: str
    started_at: str
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    ordered_commit_ids: tuple[str, ...]
    remote_name: str
    github_host: str
    github_owner: str
    github_repo: str
    bookmarks: dict[str, str]

    def change_ids(self) -> frozenset[str]:
        """Return the change IDs mentioned by this operation."""

        return frozenset(self.ordered_change_ids)


@dataclass(frozen=True, slots=True)
class RelinkOperationRecord:
    """Journal-backed recovery record for one incomplete `relink` operation."""

    kind: Literal["relink"]
    path: Path
    pid: int
    label: str
    started_at: str
    change_id: str

    def change_ids(self) -> frozenset[str]:
        """Return the change IDs mentioned by this operation."""

        return frozenset([self.change_id])


@dataclass(frozen=True, slots=True)
class CleanupOperationRecord:
    """Journal-backed recovery record for one incomplete repo cleanup operation."""

    kind: Literal["cleanup"]
    path: Path
    pid: int
    label: str
    started_at: str

    def change_ids(self) -> frozenset[str]:
        """Return the change IDs mentioned by this operation."""

        return frozenset()


@dataclass(frozen=True, slots=True)
class CleanupRebaseOperationRecord:
    """Journal-backed recovery record for one incomplete `cleanup --rebase`."""

    kind: Literal["cleanup-rebase"]
    path: Path
    pid: int
    label: str
    started_at: str
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    ordered_commit_ids: tuple[str, ...]

    def change_ids(self) -> frozenset[str]:
        """Return the change IDs mentioned by this operation."""

        return frozenset(self.ordered_change_ids)


@dataclass(frozen=True, slots=True)
class CloseOperationRecord:
    """Journal-backed recovery record for one incomplete `close` operation."""

    kind: Literal["close"]
    path: Path
    pid: int
    label: str
    started_at: str
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    ordered_commit_ids: tuple[str, ...]
    cleanup: bool

    def change_ids(self) -> frozenset[str]:
        """Return the change IDs mentioned by this operation."""

        return frozenset(self.ordered_change_ids)


type OperationRecord = (
    LandOperationRecord
    | SubmitOperationRecord
    | RelinkOperationRecord
    | CleanupOperationRecord
    | CleanupRebaseOperationRecord
    | CloseOperationRecord
)


@dataclass(frozen=True, slots=True)
class LoadedOperationRecord:
    """An incomplete journal-backed operation loaded from disk."""

    path: Path
    operation: OperationRecord

    @property
    def intent(self) -> OperationRecord:
        """Compatibility alias for older status/abort rendering paths."""

        return self.operation


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

        prune_operation_journals(state_dir)
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


def scan_incomplete_operation_records(state_dir: Path) -> list[LoadedOperationRecord]:
    """Return incomplete journal-backed operation records for the repo."""

    journal_dir = state_dir / JOURNAL_DIRNAME
    if not journal_dir.exists():
        return []
    holder = read_operation_lock_holder(state_dir)
    active_journal_path = (
        None if holder is None or holder.journal_path is None else Path(holder.journal_path)
    )
    records: list[LoadedOperationRecord] = []
    for path in sorted(journal_dir.glob("*.jsonl")):
        active_pid = 0
        if active_journal_path is not None and active_journal_path.resolve() == path.resolve():
            if holder is not None:
                active_pid = holder.pid
        try:
            record = operation_record_from_journal(path, active_pid=active_pid)
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.error("Could not parse operation journal %s: %s", path, error)
            continue
        if record is not None:
            records.append(LoadedOperationRecord(path=path, operation=record))
    return records


def operation_record_from_journal(
    path: Path,
    *,
    active_pid: int = 0,
) -> OperationRecord | None:
    """Parse one incomplete operation record from a journal, if present."""

    events = read_journal(path)
    if not events:
        raise ValueError(f"Journal is empty: {path}")
    first = events[0]
    if any(event.event in {"completed", "abandoned"} for event in events):
        return None
    if first.operation == "land":
        return land_operation_record_from_events(path, events, active_pid=active_pid)
    if first.operation == "submit":
        return submit_operation_record_from_events(path, events, active_pid=active_pid)
    if first.operation == "relink":
        return relink_operation_record_from_events(path, events, active_pid=active_pid)
    if first.operation == "cleanup":
        return cleanup_operation_record_from_events(path, events, active_pid=active_pid)
    if first.operation == "cleanup-rebase":
        return cleanup_rebase_operation_record_from_events(
            path,
            events,
            active_pid=active_pid,
        )
    if first.operation == "close":
        return close_operation_record_from_events(path, events, active_pid=active_pid)
    return None


def land_operation_record_from_journal(
    path: Path,
    *,
    active_pid: int = 0,
) -> LandOperationRecord | None:
    """Parse one incomplete land operation record from a journal, if present."""

    events = read_journal(path)
    if any(event.event in {"completed", "abandoned"} for event in events):
        return None
    return land_operation_record_from_events(path, events, active_pid=active_pid)


def land_operation_record_from_events(
    path: Path,
    events: tuple[JournalEvent, ...],
    *,
    active_pid: int = 0,
) -> LandOperationRecord:
    """Parse one land operation record from journal events."""

    if not events:
        raise ValueError(f"Journal is empty: {path}")
    first = events[0]
    if first.operation != "land":
        raise ValueError(f"Journal is not a land operation: {path}")
    if first.event != "begin":
        raise ValueError(f"Journal does not start with begin: {path}")

    options = _require_mapping(first.data.get("options"), "options")
    resolved_scope = _require_mapping(first.data.get("resolved_scope"), "resolved_scope")
    planned_revisions = tuple(
        _require_mapping(item, "planned_revisions item")
        for item in _require_sequence(
            resolved_scope.get("planned_revisions"),
            "planned_revisions",
        )
    )
    ordered_change_ids = _string_tuple(
        resolved_scope.get("ordered_change_ids"),
        "ordered_change_ids",
    )
    ordered_commit_ids = _string_tuple(
        resolved_scope.get("ordered_commit_ids"),
        "ordered_commit_ids",
    )
    landed_change_ids = _string_tuple(
        resolved_scope.get("landed_change_ids", resolved_scope.get("planned_change_ids")),
        "landed_change_ids",
    )
    selected_pr_number = options.get("selected_pr_number")
    if selected_pr_number is not None:
        selected_pr_number = int(selected_pr_number)
    display_revset = str(resolved_scope["selected_revset"])
    return LandOperationRecord(
        kind="land",
        path=path,
        pid=active_pid,
        label=f"land on {display_revset}",
        started_at=first.timestamp,
        bypass_readiness=bool(options["bypass_readiness"]),
        cleanup_bookmarks=bool(options["cleanup_bookmarks"]),
        display_revset=display_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        landed_change_ids=landed_change_ids,
        landed_bookmarks={
            str(item["change_id"]): str(item["bookmark"]) for item in planned_revisions
        },
        landed_bookmark_managed={
            str(item["change_id"]): bool(item["bookmark_managed"])
            for item in planned_revisions
        },
        landed_commit_ids={
            str(item["change_id"]): str(item["commit_id"]) for item in planned_revisions
        },
        landed_pull_request_numbers={
            str(item["change_id"]): int(item["pull_request_number"])
            for item in planned_revisions
        },
        landed_subjects={
            str(item["change_id"]): str(item["subject"]) for item in planned_revisions
        },
        trunk_branch=str(resolved_scope["trunk_branch"]),
        landed_commit_id=str(resolved_scope["landed_commit_id"]),
        selected_pr_number=selected_pr_number,
    )


def relink_operation_record_from_events(
    path: Path,
    events: tuple[JournalEvent, ...],
    *,
    active_pid: int = 0,
) -> RelinkOperationRecord:
    """Parse one relink operation record from journal events."""

    if not events:
        raise ValueError(f"Journal is empty: {path}")
    first = events[0]
    if first.operation != "relink":
        raise ValueError(f"Journal is not a relink operation: {path}")
    if first.event != "begin":
        raise ValueError(f"Journal does not start with begin: {path}")

    resolved_scope = _require_mapping(first.data.get("resolved_scope"), "resolved_scope")
    change_id = str(resolved_scope["change_id"])
    return RelinkOperationRecord(
        kind="relink",
        path=path,
        pid=active_pid,
        label=f"relink for {change_id[:8]}",
        started_at=first.timestamp,
        change_id=change_id,
    )


def submit_operation_record_from_events(
    path: Path,
    events: tuple[JournalEvent, ...],
    *,
    active_pid: int = 0,
) -> SubmitOperationRecord:
    """Parse one submit operation record from journal events."""

    if not events:
        raise ValueError(f"Journal is empty: {path}")
    first = events[0]
    if first.operation != "submit":
        raise ValueError(f"Journal is not a submit operation: {path}")
    if first.event != "begin":
        raise ValueError(f"Journal does not start with begin: {path}")

    options = _require_mapping(first.data.get("options"), "options")
    resolved_scope = _require_mapping(first.data.get("resolved_scope"), "resolved_scope")
    display_revset = str(resolved_scope["selected_revset"])
    ordered_change_ids = _string_tuple(
        resolved_scope.get("ordered_change_ids"),
        "ordered_change_ids",
    )
    ordered_commit_ids = _string_tuple(
        resolved_scope.get("ordered_commit_ids"),
        "ordered_commit_ids",
    )
    bookmarks = _require_mapping(resolved_scope.get("bookmarks"), "bookmarks")
    label = (
        f"submit for {ordered_change_ids[-1][:8]} (from {display_revset})"
        if ordered_change_ids
        else f"submit (from {display_revset})"
    )
    return SubmitOperationRecord(
        kind="submit",
        path=path,
        pid=active_pid,
        label=label,
        started_at=first.timestamp,
        display_revset=display_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        remote_name=str(options["remote_name"]),
        github_host=str(options["github_host"]),
        github_owner=str(options["github_owner"]),
        github_repo=str(options["github_repo"]),
        bookmarks={str(change_id): str(bookmark) for change_id, bookmark in bookmarks.items()},
    )


def cleanup_operation_record_from_events(
    path: Path,
    events: tuple[JournalEvent, ...],
    *,
    active_pid: int = 0,
) -> CleanupOperationRecord:
    """Parse one cleanup operation record from journal events."""

    if not events:
        raise ValueError(f"Journal is empty: {path}")
    first = events[0]
    if first.operation != "cleanup":
        raise ValueError(f"Journal is not a cleanup operation: {path}")
    if first.event != "begin":
        raise ValueError(f"Journal does not start with begin: {path}")

    return CleanupOperationRecord(
        kind="cleanup",
        path=path,
        pid=active_pid,
        label="cleanup",
        started_at=first.timestamp,
    )


def cleanup_rebase_operation_record_from_events(
    path: Path,
    events: tuple[JournalEvent, ...],
    *,
    active_pid: int = 0,
) -> CleanupRebaseOperationRecord:
    """Parse one cleanup-rebase operation record from journal events."""

    if not events:
        raise ValueError(f"Journal is empty: {path}")
    first = events[0]
    if first.operation != "cleanup-rebase":
        raise ValueError(f"Journal is not a cleanup-rebase operation: {path}")
    if first.event != "begin":
        raise ValueError(f"Journal does not start with begin: {path}")

    resolved_scope = _require_mapping(first.data.get("resolved_scope"), "resolved_scope")
    display_revset = str(resolved_scope["selected_revset"])
    ordered_change_ids = _string_tuple(
        resolved_scope.get("ordered_change_ids"),
        "ordered_change_ids",
    )
    ordered_commit_ids = _string_tuple(
        resolved_scope.get("ordered_commit_ids"),
        "ordered_commit_ids",
    )
    label = (
        f"cleanup --rebase for {ordered_change_ids[-1][:8]} "
        f"(from {display_revset})"
        if ordered_change_ids
        else f"cleanup --rebase (from {display_revset})"
    )
    return CleanupRebaseOperationRecord(
        kind="cleanup-rebase",
        path=path,
        pid=active_pid,
        label=label,
        started_at=first.timestamp,
        display_revset=display_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
    )


def close_operation_record_from_events(
    path: Path,
    events: tuple[JournalEvent, ...],
    *,
    active_pid: int = 0,
) -> CloseOperationRecord:
    """Parse one close operation record from journal events."""

    if not events:
        raise ValueError(f"Journal is empty: {path}")
    first = events[0]
    if first.operation != "close":
        raise ValueError(f"Journal is not a close operation: {path}")
    if first.event != "begin":
        raise ValueError(f"Journal does not start with begin: {path}")

    options = _require_mapping(first.data.get("options"), "options")
    resolved_scope = _require_mapping(first.data.get("resolved_scope"), "resolved_scope")
    display_revset = str(resolved_scope["selected_revset"])
    ordered_change_ids = _string_tuple(
        resolved_scope.get("ordered_change_ids"),
        "ordered_change_ids",
    )
    ordered_commit_ids = _string_tuple(
        resolved_scope.get("ordered_commit_ids"),
        "ordered_commit_ids",
    )
    cleanup = bool(options["cleanup"])
    command = "close --cleanup" if cleanup else "close"
    label = (
        f"{command} for {ordered_change_ids[-1][:8]} (from {display_revset})"
        if ordered_change_ids
        else f"{command} (from {display_revset})"
    )
    return CloseOperationRecord(
        kind="close",
        path=path,
        pid=active_pid,
        label=label,
        started_at=first.timestamp,
        display_revset=display_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        cleanup=cleanup,
    )


def append_abandoned_event(
    path: Path,
    *,
    reason: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Mark an incomplete operation journal as abandoned."""

    journal = OperationJournal.open(path)
    payload = {"reason": reason}
    if data:
        payload.update(data)
    journal.append("abandoned", payload)


def prune_operation_journals(
    state_dir: Path,
    *,
    now: datetime | None = None,
) -> None:
    """Prune retained journals while keeping recent files and a minimum count."""

    journal_dir = state_dir / JOURNAL_DIRNAME
    if not journal_dir.exists():
        return
    current_time = now or datetime.now(UTC)
    cutoff = current_time - MIN_RETAINED_JOURNAL_AGE
    journal_paths = sorted(
        (path for path in journal_dir.glob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    newest = set(journal_paths[:MIN_RETAINED_JOURNALS])
    recent = {
        path
        for path in journal_paths
        if datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff
    }
    keep = newest | recent
    for path in journal_paths:
        if path not in keep:
            path.unlink(missing_ok=True)


def _journal_path(state_dir: Path, *, operation: str, operation_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d-%H-%M-%S")
    filename = f"{timestamp}-{operation}-{operation_id}.jsonl"
    return state_dir / JOURNAL_DIRNAME / filename


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"Journal field {label} must be an object")
    return dict(value)


def _require_sequence(value: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(value, list | tuple):
        raise TypeError(f"Journal field {label} must be a sequence")
    return tuple(value)


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    return tuple(str(item) for item in _require_sequence(value, label))


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
