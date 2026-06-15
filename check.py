#!/usr/bin/env python3
"""Run the standard local verification checks for this repository."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from argparse import ArgumentParser
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = REPO_ROOT / ".venv" / (
    Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
)
PytestJobs = int | Literal["auto"]
_FRAGILE_TEST_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "use output assertion helpers instead of exact captured output equality",
        re.compile(r"captured\.(?:out|err)\s*=="),
    ),
    (
        "avoid exact splitlines() assertions for captured console output",
        re.compile(
            r"(?:captured\.(?:out|err)|(?:stdout|stderr)\.getvalue\(\))"
            r"\.splitlines\(\)\s*=="
        ),
    ),
    (
        "avoid asserting exact rendered indentation for wrapped output",
        re.compile(r"""startswith\(["'] {4}["']\)"""),
    ),
    (
        "avoid asserting whole rendered outputs are byte-for-byte identical",
        re.compile(r"\.out\s*==\s*.*\.out"),
    ),
)


def _parse_pytest_jobs(value: str) -> PytestJobs:
    if value == "auto":
        return "auto"
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError("--pytest-jobs must be a positive integer or 'auto'") from error
    if parsed < 1:
        raise ValueError("--pytest-jobs must be a positive integer or 'auto'")
    return parsed


def _build_checks(
    *,
    pytest_jobs: PytestJobs | None,
    coverage: bool,
    concurrency_report: bool,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    pytest_command: tuple[str, ...] = ("-m", "pytest", *_pytest_basetemp_args())
    if pytest_jobs in (None, "auto"):
        pytest_command = (*pytest_command, "-n", "auto")
    elif isinstance(pytest_jobs, int) and pytest_jobs > 1:
        pytest_command = (*pytest_command, "-n", str(pytest_jobs))
    if concurrency_report:
        pytest_command = (*pytest_command, "--concurrency-report")
    if coverage:
        pytest_command = (
            *pytest_command,
            "--cov=jj_stack",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
        )
    return (
        ("ruff", ("-m", "ruff", "check")),
        ("pyrefly", ("-m", "pyrefly", "check")),
        ("pytest", pytest_command),
    )


def _pytest_basetemp_args() -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    base_temp_root = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    return ("--basetemp", str(base_temp_root / "pt"))


def main(argv: Sequence[str] | None = None) -> int:
    """Run Ruff, Pyrefly, and the test suite in sequence."""

    parser = ArgumentParser(
        prog="check.py",
        description="Run the local Ruff, pyrefly, and pytest checks.",
    )
    parser.add_argument(
        "-n",
        "--pytest-jobs",
        metavar="N",
        help="Run pytest with xdist using N workers or 'auto' (default: auto).",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help=(
            "Run pytest with branch coverage enabled and emit terminal and "
            "HTML reports in htmlcov/."
        ),
    )
    parser.add_argument(
        "--pytest-concurrency-report",
        action="store_true",
        help="Report observed test concurrency and highlight bottlenecks.",
    )
    args = parser.parse_args(argv)
    try:
        pytest_jobs = (
            None if args.pytest_jobs is None else _parse_pytest_jobs(args.pytest_jobs)
        )
    except ValueError as error:
        parser.error(str(error))
    ensure_project_environment()
    _check_fragile_test_output_assertions()
    command_env = _project_command_env()

    for name, command in _build_checks(
        pytest_jobs=pytest_jobs,
        coverage=args.coverage,
        concurrency_report=args.pytest_concurrency_report,
    ):
        full_command = (str(VENV_PYTHON), *command)
        print(f"==> {name}: {shlex.join(full_command)}", flush=True)
        completed = subprocess.run(
            full_command,
            check=False,
            cwd=REPO_ROOT,
            env=command_env,
        )
        if completed.returncode != 0:
            return completed.returncode

    return 0


def ensure_project_environment() -> None:
    """Refresh the project virtualenv before running the verification suite."""

    sync_command = ("uv", "sync", "--locked")
    print(f"==> bootstrap: {shlex.join(sync_command)}", flush=True)
    completed = subprocess.run(
        sync_command,
        check=False,
        cwd=REPO_ROOT,
        env=_project_command_env(),
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _check_fragile_test_output_assertions() -> None:
    """Reject test assertions that are too sensitive to terminal rendering."""

    violations: list[str] = []
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            for reason, pattern in _FRAGILE_TEST_OUTPUT_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {reason}")
                    break
    if not violations:
        return

    joined = "\n".join(violations)
    raise SystemExit(
        "Error: fragile test output assertions are not allowed.\n"
        "Prefer tests.support.output_assertions helpers or semantic content checks.\n"
        f"{joined}"
    )


def _project_command_env() -> dict[str, str]:
    """Return a subprocess environment pinned to the project virtualenv."""

    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


if __name__ == "__main__":
    raise SystemExit(main())
