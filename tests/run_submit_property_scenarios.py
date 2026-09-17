#!/usr/bin/env python3
"""Run generated jj-stack command sequences on parallel pytest workers."""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
import sys
from argparse import ArgumentParser, ArgumentTypeError
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = int(os.environ.get("JJ_STACK_PROPERTY_EXAMPLES", "1"))
STEPS = int(os.environ.get("JJ_STACK_PROPERTY_STEPS", "8"))
SHARDS = int(os.environ.get("JJ_STACK_PROPERTY_SHARDS", "1"))
SEED = int(os.environ.get("JJ_STACK_PROPERTY_SEED", "8675309"))


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ArgumentTypeError("expected a positive integer") from error
    if parsed < 1:
        raise ArgumentTypeError("expected a positive integer")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "examples",
        nargs="?",
        type=positive_int,
        default=5,
        help="Generated examples per shard (default: 5).",
    )
    parser.add_argument(
        "--steps",
        type=positive_int,
        default=20,
        help="Maximum actions per example (default: 20).",
    )
    parser.add_argument(
        "--shards",
        type=positive_int,
        help="Independent seeded searches (default: four per worker).",
    )
    seeds = parser.add_mutually_exclusive_group()
    seeds.add_argument("--seed", type=int, default=None)
    seeds.add_argument("--random-seed", action="store_true")
    parser.add_argument(
        "-n", "--jobs", default="auto", help="pytest workers (default: available CPUs)."
    )
    parser.add_argument("--no-sync", action="store_true", help="Skip uv sync --locked.")
    arguments = list(sys.argv[1:] if argv is None else argv)
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:separator])
    pytest_args = arguments[separator + 1 :]
    if args.jobs == "auto":
        jobs = os.process_cpu_count() or 1
    else:
        try:
            jobs = positive_int(args.jobs)
        except ArgumentTypeError as error:
            parser.error(f"--jobs: {error}")
    shards = args.shards if args.shards is not None else 4 * jobs
    chosen_seed = secrets.randbits(32) if args.random_seed else args.seed
    if chosen_seed is None:
        chosen_seed = SEED
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    env.update(
        {
            "JJ_STACK_PROPERTY_EXAMPLES": str(args.examples),
            "JJ_STACK_PROPERTY_STEPS": str(args.steps),
            "JJ_STACK_PROPERTY_SHARDS": str(shards),
            "JJ_STACK_PROPERTY_SEED": str(chosen_seed),
        }
    )
    if not args.no_sync:
        result = subprocess.run(("uv", "sync", "--locked"), cwd=REPO_ROOT, env=env)
        if result.returncode:
            return result.returncode
    reproduce = [
        "just",
        "property",
        str(args.examples),
        "--steps",
        str(args.steps),
        "--shards",
        str(shards),
        "--seed",
        str(chosen_seed),
        "-n",
        str(jobs),
    ]
    if pytest_args:
        reproduce.extend(("--", *pytest_args))
    print(f"Reproduce: {shlex.join(reproduce)}", flush=True)
    python = REPO_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            "-n",
            str(jobs),
            "--dist=worksteal",
            f"--randomly-seed={chosen_seed}",
            "tests/property/test_submit_property_scenarios.py",
            *pytest_args,
        ],
        cwd=REPO_ROOT,
        env=env,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
