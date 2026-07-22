#!/usr/bin/env python3
"""Check the repository's merger complexity budgets."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "complexity-budget.toml"
SLOC_TOTAL = re.compile(r"Total Physical Source Lines of Code \(SLOC\)\s*=\s*([\d,]+)")


def _run(
    command: Sequence[str],
    *,
    accepted: tuple[int, ...] = (0,),
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        command,
        check=False,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode not in accepted:
        raise SystemExit(f"Error: {' '.join(command)} failed:\n{result.stdout}")
    return result.stdout


def _sloc(paths: Sequence[str], data_dir: str) -> int:
    output = _run(("sloccount", "--datadir", data_dir, *paths), accepted=(0, 1))
    if "SLOC total is zero" in output:
        return 0
    match = SLOC_TOTAL.search(output)
    if match is None:
        raise SystemExit(f"Error: sloccount returned no total for {', '.join(paths)}:\n{output}")
    return int(match.group(1).replace(",", ""))


def _c901(paths: Sequence[str]) -> int:
    output = _run(
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            *paths,
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=10",
            "--output-format",
            "concise",
        ),
        accepted=(0, 1),
    )
    return sum(" C901 " in line for line in output.splitlines())


def _collected(marker: str, paths: Sequence[str]) -> int:
    output = _run(
        (
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            marker,
            *paths,
        ),
        env=_collection_env(),
    )
    return sum(line.startswith("tests/") and "::" in line for line in output.splitlines())


def _collection_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key != "PYTEST_ADDOPTS"
        and not (key.startswith("JJ_STACK_") and "PROPERTY_" in key)
    }


def main() -> int:
    """Measure the governed surfaces and reject budget overruns."""

    if shutil.which("sloccount") is None:
        raise SystemExit(
            "Error: sloccount is required. Install it, then rerun "
            "uv run tools/check_complexity.py."
        )
    budget = tomllib.loads(BUDGET.read_text(encoding="utf-8"))
    paths = budget["paths"]
    missing = [
        relative
        for configured_paths in paths.values()
        for relative in configured_paths
        if not (ROOT / relative).exists()
    ]
    if missing:
        raise SystemExit(f"Error: missing complexity-budget paths: {', '.join(missing)}")
    limits = budget["sloc"] | budget["ruff"] | budget["pytest"]
    with tempfile.TemporaryDirectory(prefix="jj-stack-sloc-") as data_dir:
        measured = {
            "production": _sloc(paths["production"], data_dir),
            "tests": _sloc(paths["tests"], data_dir),
            "checker": _sloc(paths["checker"], data_dir),
            "land": _sloc(paths["land"], data_dir),
            "governed": _sloc(paths["governed"], data_dir),
        }
        measured["total"] = measured["production"] + measured["tests"]
        modules = sorted(
            path
            for relative in paths["governed"]
            for path in (
                (ROOT / relative).rglob("*.py")
                if (ROOT / relative).is_dir()
                else (ROOT / relative,)
            )
        )
        module_sloc = {
            path.relative_to(ROOT): _sloc((str(path.relative_to(ROOT)),), data_dir)
            for path in set(modules)
        }
    measured |= {
        "c901": _c901(("src/jj_stack",)),
        "governed_c901": _c901(paths["governed"]),
        "fixed_property": _collected("fixed_property", paths["fixed_property"]),
        "merger_replacement": _collected(
            "merger_replacement", paths["merger_replacement"]
        ),
    }

    failures: list[str] = []
    for name, value in measured.items():
        print(f"{name}: {value:,} / {limits[name]:,}")
        if value > limits[name]:
            failures.append(f"{name}: {value:,} > {limits[name]:,}")
    for path, value in sorted(module_sloc.items()):
        limit = limits["governed_module"]
        print(f"{path}: {value:,} / {limit:,}")
        if value > limit:
            failures.append(f"{path}: {value:,} > {limit:,}")
    if not failures:
        return 0
    print("\nComplexity budget exceeded:\n- " + "\n- ".join(failures), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
