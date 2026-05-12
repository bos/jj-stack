"""Repo-scoped operation lock for jj-review state mutations."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from jj_review.errors import CliError
from jj_review.system import pid_is_alive

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
        holder: OperationLockHolder,
        holder_path: Path,
    ) -> None:
        self._file = file
        self.holder = holder
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
        _clear_holder_if_owned(self._holder_path, self.holder)
        _unlock_file(self._file)
        self._file.close()


def acquire_operation_lock(
    state_dir: Path,
    *,
    command: str,
    poll_interval: float = DEFAULT_LOCK_POLL_SECONDS,
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
            raise CliError(_operation_lock_busy_message(holder))
        sleep_for = min(poll_interval, max(0.0, deadline - time.monotonic()))
        if sleep_for:
            time.sleep(sleep_for)


def try_acquire_operation_lock(
    state_dir: Path,
    *,
    command: str,
) -> OperationLock | None:
    """Try to acquire the repo operation lock without blocking."""

    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / LOCK_FILENAME
    holder_path = state_dir / HOLDER_FILENAME
    lock_file = _open_lock_file(lock_path)
    if not _try_lock_file(lock_file):
        lock_file.close()
        return None

    try:
        _cleanup_dead_holder(holder_path)
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
    return OperationLock(file=lock_file, holder=holder, holder_path=holder_path)


def read_operation_lock_holder(state_dir: Path) -> OperationLockHolder | None:
    """Return the recorded lock holder, if the companion file is readable."""

    holder_path = state_dir / HOLDER_FILENAME
    try:
        raw = json.loads(holder_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return OperationLockHolder(
            command=str(raw["command"]),
            pid=int(raw["pid"]),
            started_at=str(raw["started_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _open_lock_file(lock_path: Path):
    lock_file = lock_path.open("a+b")
    if sys.platform == "win32":
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
    return lock_file


def _try_lock_file(lock_file) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise

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


def _unlock_file(lock_file) -> None:
    if sys.platform == "win32":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

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
    except Exception:
        Path(tmp_path_str).unlink(missing_ok=True)
        raise


def _cleanup_dead_holder(holder_path: Path) -> None:
    holder = read_operation_lock_holder(holder_path.parent)
    if holder is not None and not pid_is_alive(holder.pid):
        holder_path.unlink(missing_ok=True)


def _clear_holder_if_owned(holder_path: Path, holder: OperationLockHolder) -> None:
    current = read_operation_lock_holder(holder_path.parent)
    if current == holder:
        holder_path.unlink(missing_ok=True)


def _operation_lock_busy_message(holder: OperationLockHolder | None) -> str:
    if holder is None:
        return "Another jj-review operation is already running."
    if not pid_is_alive(holder.pid):
        return (
            f"Operation lock is held but the recorded {holder.command} holder "
            f"(PID {holder.pid}, started {holder.started_at}) is no longer running. "
            f"Wait for the previous process to exit or retry shortly."
        )
    return (
        f"Another jj-review {holder.command} operation is already running "
        f"(PID {holder.pid}, started {holder.started_at})."
    )
