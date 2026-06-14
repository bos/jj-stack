#!/usr/bin/env python3
"""Run the opt-in submit property scenario suite."""

from __future__ import annotations

import os
import shlex
import subprocess
from argparse import ArgumentParser, ArgumentTypeError
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROPERTY_TEST_FILE = REPO_ROOT / "tests" / "property" / "submit_property_scenarios.py"


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="tests/run_submit_property_scenarios.py",
        description="Run opt-in submit property scenarios with pytest-xdist.",
    )
    parser.add_argument(
        "scenarios",
        nargs="?",
        type=_positive_int,
        default=100,
        help="Number of generated stack-edit scenarios to run (default: 100).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Deterministic scenario seed. Defaults to the harness seed.",
    )
    parser.add_argument(
        "--cross-stack-scenarios",
        type=_non_negative_int,
        help=(
            "Number of generated cross-stack split scenarios to run "
            "(default: max(4, scenarios // 10))."
        ),
    )
    parser.add_argument(
        "--stack-merge-scenarios",
        type=_non_negative_int,
        help=(
            "Number of generated two-stack merge scenarios to run "
            "(default: max(4, scenarios // 10))."
        ),
    )
    parser.add_argument(
        "--stack-move-scenarios",
        type=_non_negative_int,
        help=(
            "Number of generated cross-stack single-change move scenarios to run "
            "(default: max(4, scenarios // 10))."
        ),
    )
    parser.add_argument(
        "--retry-scenarios",
        type=_non_negative_int,
        help=(
            "Number of generated failed-submit retry scenarios to run "
            "(default: max(4, scenarios // 10))."
        ),
    )
    parser.add_argument(
        "-n",
        "--jobs",
        default="auto",
        help="Number of pytest-xdist workers, or 'auto' (default: auto).",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip uv sync --locked before running pytest.",
    )
    args, pytest_args = parser.parse_known_args(argv)
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]
    _validate_jobs(args.jobs, parser)

    if not args.no_sync:
        sync_command = ("uv", "sync", "--locked")
        print(f"==> bootstrap: {shlex.join(sync_command)}", flush=True)
        completed = subprocess.run(sync_command, cwd=REPO_ROOT, env=_command_env())
        if completed.returncode != 0:
            return completed.returncode

    env = _command_env()
    env.setdefault("JJ_USER", "Test User")
    env.setdefault("JJ_EMAIL", "test@example.com")
    env["JJ_STACK_SUBMIT_PROPERTY_SCENARIOS"] = str(args.scenarios)
    cross_stack_scenarios = args.cross_stack_scenarios
    if cross_stack_scenarios is None:
        cross_stack_scenarios = max(4, args.scenarios // 10)
    env["JJ_STACK_SUBMIT_PROPERTY_CROSS_STACK_SCENARIOS"] = str(cross_stack_scenarios)
    stack_merge_scenarios = args.stack_merge_scenarios
    if stack_merge_scenarios is None:
        stack_merge_scenarios = max(4, args.scenarios // 10)
    env["JJ_STACK_SUBMIT_PROPERTY_STACK_MERGE_SCENARIOS"] = str(stack_merge_scenarios)
    stack_move_scenarios = args.stack_move_scenarios
    if stack_move_scenarios is None:
        stack_move_scenarios = max(4, args.scenarios // 10)
    env["JJ_STACK_SUBMIT_PROPERTY_STACK_MOVE_SCENARIOS"] = str(stack_move_scenarios)
    retry_scenarios = args.retry_scenarios
    if retry_scenarios is None:
        retry_scenarios = max(4, args.scenarios // 10)
    env["JJ_STACK_SUBMIT_PROPERTY_RETRY_SCENARIOS"] = str(retry_scenarios)
    if args.seed is not None:
        env["JJ_STACK_SUBMIT_PROPERTY_SEED"] = str(args.seed)

    venv_python = REPO_ROOT / ".venv" / (
        Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    )
    command = [
        str(venv_python),
        "-m",
        "pytest",
        "-n",
        args.jobs,
        str(PROPERTY_TEST_FILE.relative_to(REPO_ROOT)),
        *pytest_args,
    ]
    print(f"==> property scenarios: {shlex.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=REPO_ROOT, env=env)
    return completed.returncode


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ArgumentTypeError("scenario count must be a positive integer") from error
    if parsed < 1:
        raise ArgumentTypeError("scenario count must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ArgumentTypeError("scenario count must be a non-negative integer") from error
    if parsed < 0:
        raise ArgumentTypeError("scenario count must be a non-negative integer")
    return parsed


def _validate_jobs(value: str, parser: ArgumentParser) -> None:
    if value == "auto":
        return
    try:
        parsed = int(value)
    except ValueError:
        parser.error("--jobs must be a positive integer or 'auto'")
    if parsed < 1:
        parser.error("--jobs must be a positive integer or 'auto'")


def _command_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


if __name__ == "__main__":
    raise SystemExit(main())
