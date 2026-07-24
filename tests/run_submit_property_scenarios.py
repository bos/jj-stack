#!/usr/bin/env python3
"""Run the opt-in stack property scenario suites."""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
from argparse import ArgumentParser, ArgumentTypeError
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROPERTY_TEST_FILES = (REPO_ROOT / "tests" / "property" / "submit_property_scenarios.py",)
DEFAULT_PROPERTY_SEED = 8675309
_REPRODUCTION_SCENARIO_OPTIONS = (
    (
        "--stack-merge-scenarios",
        "JJ_STACK_SUBMIT_PROPERTY_STACK_MERGE_SCENARIOS",
    ),
    (
        "--stack-move-scenarios",
        "JJ_STACK_SUBMIT_PROPERTY_STACK_MOVE_SCENARIOS",
    ),
    ("--retry-scenarios", "JJ_STACK_SUBMIT_PROPERTY_RETRY_SCENARIOS"),
    ("--drift-scenarios", "JJ_STACK_SUBMIT_PROPERTY_DRIFT_SCENARIOS"),
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="tests/run_submit_property_scenarios.py",
        description="Run opt-in stack property scenarios with pytest-xdist.",
    )
    parser.add_argument(
        "scenarios",
        nargs="?",
        type=_positive_int,
        default=100,
        help="Number of generated stack-edit scenarios to run (default: 100).",
    )
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument(
        "--seed",
        type=int,
        help="Deterministic scenario seed. Defaults to the harness seed.",
    )
    seed_group.add_argument(
        "--random-seed",
        action="store_true",
        help="Generate and print one random seed for scenarios and pytest ordering.",
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
        "--drift-scenarios",
        type=_non_negative_int,
        help=(
            "Number of generated external-drift scenarios to run "
            "(default: max(20, scenarios // 5))."
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
    drift_scenarios = args.drift_scenarios
    if drift_scenarios is None:
        drift_scenarios = max(20, args.scenarios // 5)
    env["JJ_STACK_SUBMIT_PROPERTY_DRIFT_SCENARIOS"] = str(drift_scenarios)
    seed = secrets.randbits(32) if args.random_seed else args.seed
    if seed is None:
        seed = DEFAULT_PROPERTY_SEED
    env["JJ_STACK_SUBMIT_PROPERTY_SEED"] = str(seed)

    venv_python = (
        REPO_ROOT
        / ".venv"
        / (Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python"))
    )
    test_files = [str(path.relative_to(REPO_ROOT)) for path in PROPERTY_TEST_FILES]
    command = [
        str(venv_python),
        "-m",
        "pytest",
        "-n",
        args.jobs,
        f"--randomly-seed={seed}",
        *test_files,
        *pytest_args,
    ]
    reproduction_command = _build_reproduction_command(
        env=env,
        jobs=args.jobs,
        no_sync=args.no_sync,
        pytest_args=pytest_args,
        scenarios=args.scenarios,
        seed=seed,
    )

    print(f"==> property seed: {seed}", flush=True)
    print(f"==> reproduce: {shlex.join(reproduction_command)}", flush=True)
    print(f"==> property scenarios: {shlex.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=REPO_ROOT, env=env)
    return completed.returncode


def _build_reproduction_command(
    *,
    env: Mapping[str, str],
    jobs: str,
    no_sync: bool,
    pytest_args: Sequence[str],
    scenarios: int,
    seed: int,
) -> tuple[str, ...]:
    command = [
        "tests/run_submit_property_scenarios.py",
        str(scenarios),
        "--seed",
        str(seed),
        "--jobs",
        jobs,
    ]
    for option, environment_name in _REPRODUCTION_SCENARIO_OPTIONS:
        command.extend((option, env[environment_name]))
    if no_sync:
        command.append("--no-sync")
    if pytest_args:
        command.extend(("--", *pytest_args))
    return tuple(command)


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
