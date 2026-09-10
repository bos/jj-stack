"""Repo-scoped operation lock for jj-stack state mutations."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from jj_stack.errors import CliError
from jj_stack.state.store import TrackingStore

LOCK_FILENAME = "operation.lock"
HOLDER_FILENAME = "operation-lock.json"
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_LOCK_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class OperationLockHolder:
    """Diagnostic metadata for the process that owns the operation lock."""

    command: str
    pid: int
    started_at: str


class OperationLock:
    """Held operation lock.

    The lock is advisory and process-scoped. Keep this object alive for the whole
    operation; closing it releases the underlying OS lock.
    """

    def __init__(
        self,
        *,
        file,
        holder_path: Path,
    ) -> None:
        self._file = file
        self._holder_path = holder_path
        self._released = False

    def __enter__(self) -> OperationLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def release(self) -> None:
        """Release the OS lock and remove our holder metadata."""

        if self._released:
            return
        self._released = True
        self._holder_path.unlink(missing_ok=True)
        _unlock_file(self._file)
        self._file.close()


def acquire_operation_lock(
    state_dir: Path,
    *,
    command: str,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> OperationLock:
    """Acquire the repo operation lock, waiting briefly before failing closed."""

    deadline = time.monotonic() + timeout
    while True:
        lock = try_acquire_operation_lock(
            state_dir,
            command=command,
        )
        if lock is not None:
            return lock
        if time.monotonic() >= deadline:
            holder = read_operation_lock_holder(state_dir)
            raise CliError(
                _operation_lock_busy_message(state_dir, holder),
                hint="Wait for that command to finish, then retry.",
            )
        sleep_for = min(DEFAULT_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
        if sleep_for:
            time.sleep(sleep_for)


@contextmanager
def operation_lock(
    state_store: TrackingStore,
    *,
    command: str,
    mutating: bool = True,
) -> Iterator[None]:
    """Serialize a mutating command; a read-only run takes no lock and creates no state.

    Acquiring the lock also creates the data directory, so callers under it need no separate
    writability check before persisting tracking.
    """

    if not mutating:
        yield
        return
    with acquire_operation_lock(state_store.require_writable(), command=command):
        yield


def try_acquire_operation_lock(
    state_dir: Path,
    *,
    command: str,
) -> OperationLock | None:
    """Try to acquire the repo operation lock without blocking."""

    lock_path = state_dir / LOCK_FILENAME
    holder_path = state_dir / HOLDER_FILENAME
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        lock_file = _open_lock_file(lock_path)
        if not _try_lock_file(lock_file):
            lock_file.close()
            return None
        try:
            holder = OperationLockHolder(
                command=command,
                pid=os.getpid(),
                started_at=datetime.now(UTC).isoformat(),
            )
            _write_holder(holder_path, holder)
        except BaseException:
            _unlock_file(lock_file)
            lock_file.close()
            raise
    except OSError as error:
        raise CliError(
            f"Could not use jj-stack data directory {state_dir}: {error}",
            hint="Resolve the filesystem error above, then rerun the command.",
        ) from error
    return OperationLock(file=lock_file, holder_path=holder_path)


def read_operation_lock_holder(state_dir: Path) -> OperationLockHolder | None:
    """Return the recorded lock holder, if the companion file is readable."""

    holder_path = state_dir / HOLDER_FILENAME
    try:
        raw = json.loads(holder_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    try:
        return OperationLockHolder(
            command=str(raw["command"]),
            pid=int(raw["pid"]),
            started_at=str(raw["started_at"]),
        )
    except KeyError, TypeError, ValueError:
        return None


def _open_lock_file(lock_path: Path) -> BinaryIO:
    lock_file = lock_path.open("a+b")
    if sys.platform == "win32":
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
    return lock_file


def _try_lock_file(lock_file: BinaryIO) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
    else:
        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise


def _unlock_file(lock_file: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_holder(holder_path: Path, holder: OperationLockHolder) -> None:
    holder_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=holder_path.parent,
        prefix=holder_path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(asdict(holder), tmp, indent=2)
            tmp.write("\n")
        Path(tmp_path_str).replace(holder_path)
    except OSError:
        Path(tmp_path_str).unlink(missing_ok=True)
        raise


def _operation_lock_busy_message(state_dir: Path, holder: OperationLockHolder | None) -> str:
    if holder is None:
        return f"Another jj-stack command is using this repo (lock: {state_dir / LOCK_FILENAME})."
    return (
        f"jj-stack {holder.command} is already running in this repo "
        f"(PID {holder.pid}, started {holder.started_at})."
    )
