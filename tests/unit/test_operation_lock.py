from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from jj_review.state.operation_lock import (
    HOLDER_FILENAME,
    acquire_operation_lock,
    read_operation_lock_holder,
)


def _run_lock_script(script: str, state_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, str(state_dir)],
        capture_output=True,
        check=False,
        text=True,
    )


def test_operation_lock_blocks_another_process_with_holder_diagnostic(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with acquire_operation_lock(state_dir, command="parent", timeout=0.1):
        completed = _run_lock_script(
            """
from pathlib import Path
import sys
from jj_review.errors import CliError
from jj_review.state.operation_lock import acquire_operation_lock

try:
    with acquire_operation_lock(
        Path(sys.argv[1]),
        command="child",
        poll_interval=0.02,
        timeout=0.05,
    ):
        pass
except CliError as error:
    print(error)
    raise SystemExit(7)
raise SystemExit(0)
""",
            state_dir,
        )

    assert completed.returncode == 7
    assert "parent" in completed.stdout
    assert "PID" in completed.stdout


def test_operation_lock_try_acquire_reports_busy_across_processes(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with acquire_operation_lock(state_dir, command="parent", timeout=0.1):
        completed = _run_lock_script(
            """
from pathlib import Path
import sys
from jj_review.state.operation_lock import try_acquire_operation_lock

lock = try_acquire_operation_lock(Path(sys.argv[1]), command="child")
if lock is None:
    raise SystemExit(0)
lock.release()
raise SystemExit(9)
""",
            state_dir,
        )

    assert completed.returncode == 0


def test_operation_lock_replaces_dead_pid_holder_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    holder_path = state_dir / HOLDER_FILENAME
    holder_path.write_text(
        json.dumps(
            {
                "command": "dead",
                "pid": 99_999_999,
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with acquire_operation_lock(state_dir, command="next", timeout=0.1):
        holder = read_operation_lock_holder(state_dir)

    assert holder is not None
    assert holder.command == "next"
    assert holder.pid != 99_999_999
    assert not holder_path.exists()
