#!/usr/bin/env python3
"""Run the standard local verification checks for this repository."""

from __future__ import annotations

import ast
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
VENV_PYTHON = (
    REPO_ROOT / ".venv" / (Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python"))
)
PytestJobs = int | Literal["auto"]
_TYPE_CHECK_TARGETS = ("src", "tests", "tools", "check.py")
_TYPE_CHECKERS = ("pyrefly", "ty")
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
    type_checkers: Sequence[str],
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
            "--cov",
            "--cov-report=term",
            "--cov-report=html",
        )
    type_checks: list[tuple[str, tuple[str, ...]]] = []
    for checker in type_checkers:
        command = ("-m", checker, "check")
        type_checks.append((checker, (*command, *_TYPE_CHECK_TARGETS)))
        if checker == "pyrefly":
            type_checks.append(
                (
                    "pyrefly-windows",
                    (*command, "--python-platform", "win32", *_TYPE_CHECK_TARGETS),
                )
            )
    return (
        ("source-policy", ("-c", "import check; check._check_production_assertions()")),
        ("ruff", ("-m", "ruff", "check")),
        ("ruff-format", ("-m", "ruff", "format", "--check")),
        *type_checks,
        ("pytest", pytest_command),
    )


def _pytest_basetemp_args() -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    base_temp_root = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    return ("--basetemp", str(base_temp_root / "pt"))


def main(argv: Sequence[str] | None = None) -> int:
    """Run Ruff, the selected type checkers, and the test suite in sequence."""

    parser = ArgumentParser(
        prog="check.py",
        description="Run Ruff, type checking (Pyrefly by default), and pytest.",
    )
    parser.add_argument(
        "-t",
        "--type-checker",
        choices=_TYPE_CHECKERS,
        action="append",
        help="Choose a type checker instead of the default Pyrefly; repeat to run several.",
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
        pytest_jobs = None if args.pytest_jobs is None else _parse_pytest_jobs(args.pytest_jobs)
    except ValueError as error:
        parser.error(str(error))
    type_checkers = tuple(dict.fromkeys(args.type_checker or ("pyrefly",)))
    ensure_project_environment(type_checkers)
    _check_fragile_test_output_assertions()
    command_env = _project_command_env()

    for name, command in _build_checks(
        type_checkers=type_checkers,
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


def ensure_project_environment(type_checkers: Sequence[str]) -> None:
    """Refresh the project virtualenv before running the verification suite."""

    groups = tuple(
        arg for name in type_checkers if name != "pyrefly" for arg in ("--group", name)
    )
    sync_command = ("uv", "sync", "--locked", *groups)
    print(f"==> bootstrap: {shlex.join(sync_command)}", flush=True)
    completed = subprocess.run(
        sync_command,
        check=False,
        cwd=REPO_ROOT,
        env=_project_command_env(),
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _check_production_assertions() -> None:
    """Keep runtime assertions out of production code, including explicit assertion errors."""

    violations: set[str] = set()
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            match node:
                case (
                    ast.Assert()
                    | ast.Name(id="AssertionError" | "assert_never")
                    | ast.Attribute(attr="AssertionError" | "assert_never")
                    | ast.alias(name="AssertionError" | "assert_never")
                ):
                    violations.add(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    if violations:
        raise SystemExit(
            "Error: assertions are not allowed in src. Express required inputs in types "
            "and handle reachable failures explicitly.\n" + "\n".join(sorted(violations))
        )


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
