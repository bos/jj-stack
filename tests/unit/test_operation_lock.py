from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jj_stack.state import operation_lock as operation_lock_module
from jj_stack.state.operation_lock import (
    acquire_operation_lock,
    try_acquire_operation_lock,
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
from jj_stack.errors import CliError
from jj_stack.state.operation_lock import acquire_operation_lock

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
from jj_stack.state.operation_lock import try_acquire_operation_lock

lock = try_acquire_operation_lock(Path(sys.argv[1]), command="child")
if lock is None:
    raise SystemExit(0)
lock.release()
raise SystemExit(9)
""",
            state_dir,
        )

    assert completed.returncode == 0


def test_operation_lock_releases_file_lock_when_holder_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"

    def _explode(*_args, **_kwargs) -> None:
        raise OSError("simulated holder write failure")

    monkeypatch.setattr(operation_lock_module, "_write_holder", _explode)

    with pytest.raises(OSError, match="simulated holder write failure"):
        try_acquire_operation_lock(state_dir, command="first")

    monkeypatch.undo()

    completed = _run_lock_script(
        """
from pathlib import Path
import sys
from jj_stack.state.operation_lock import try_acquire_operation_lock

lock = try_acquire_operation_lock(Path(sys.argv[1]), command="next")
if lock is None:
    raise SystemExit(2)
lock.release()
raise SystemExit(0)
""",
        state_dir,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
