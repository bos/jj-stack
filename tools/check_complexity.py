#!/usr/bin/env python3
"""Check the repo's complexity budgets."""

import ast
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "complexity-budget.toml"
TOKEI_VERSIONS = ("14", "15")


def _run(command: Sequence[str], *, accepted: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode not in accepted:
        raise SystemExit(f"Error: {' '.join(command)} failed:\n{result.stdout}")
    return result.stdout


def _docstring_lines(path: Path) -> int:
    """Count a Python file's docstring lines, which tokei reports as code.

    Docstrings are documentation, so they must not consume a code budget; the budgets would
    otherwise reward deleting them.
    """

    try:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    except OSError, SyntaxError:
        return 0
    documented = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, documented) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(getattr(first.value, "value", None), str):
            total += (first.end_lineno or first.lineno) - first.lineno + 1
    return total


def _code_lines(paths: Sequence[str]) -> int:
    output = _run(("tokei", "--output", "json", *paths))
    try:
        languages = json.loads(output)
        reports = [
            (name, report)
            for name, language in languages.items()
            if name != "Total"
            for report in language["reports"]
        ]
        total = sum(report["stats"]["code"] for _name, report in reports)
    except json.JSONDecodeError, AttributeError, KeyError, TypeError:
        raise SystemExit(
            f"Error: tokei returned invalid JSON for {', '.join(paths)}:\n{output}"
        ) from None
    return total - sum(
        _docstring_lines(Path(report["name"])) for name, report in reports if name == "Python"
    )


def _c901(paths: Sequence[str]) -> int:
    command = (sys.executable, "-m", "ruff", "check", *paths, "--select", "C901")
    options = ("--config", "lint.mccabe.max-complexity=10", "--output-format", "concise")
    output = _run(command + options, accepted=(0, 1))
    return sum(" C901 " in line for line in output.splitlines())


def _quantity(value: int, unit: str) -> str:
    return f"{value:,} {unit}{'' if value == 1 else 's'}"


def _budget_result(*, label: str, limit: int, unit: str, value: int) -> tuple[str, str | None]:
    remaining = limit - value
    if remaining < 0:
        detail = f"OVER LIMIT by {_quantity(-remaining, unit)}"
    elif limit == 0:
        detail = "requirement met"
    else:
        detail = f"{_quantity(remaining, unit)} available"
    line = f"  {label}: {_quantity(value, unit)} (limit {_quantity(limit, unit)}; {detail})"
    failure = f"{label}: {detail}" if remaining < 0 else None
    return line, failure


def _report(
    labels: Mapping[str, str],
    limits: Mapping[str, int],
    measured: Mapping[str, int],
    module_lines: Mapping[Path, int],
    units: Mapping[str, str],
) -> int:
    failures: list[str] = []
    sections = (
        ("Code size", ("production", "tests", "total", "merge", "governed")),
        ("Functions with a complexity score above 10", ("c901", "governed_c901")),
    )
    print("Complexity check")
    for heading, names in sections:
        print(f"\n{heading}")
        for name in names:
            line, failure = _budget_result(
                label=labels[name], limit=limits[name], unit=units[name], value=measured[name]
            )
            print(line)
            if failure is not None:
                failures.append(failure)
    ordered_modules = sorted(module_lines.items(), key=lambda item: (-item[1], str(item[0])))
    module_results = tuple(
        _budget_result(label=str(path), limit=limits["governed_module"], unit="line", value=value)
        for path, value in ordered_modules
    )
    module_failures = tuple(failure for _, failure in module_results if failure is not None)
    failures.extend(module_failures)
    print(
        f"\nMerge/recovery file sizes ({_quantity(len(module_results), 'file')}; "
        f"{limits['governed_module']:,}-line limit each)"
    )
    if not module_failures:
        print("  All files are within the limit.")
    for line, failure in module_results:
        if failure is not None:
            print(f"  {line}")
    if failures:
        sys.stdout.flush()
        print("\nResult: failed\n- " + "\n- ".join(failures), file=sys.stderr)
        return 1
    print(f"\nResult: all {len(measured) + len(module_lines)} limits passed.")
    return 0


def main() -> int:
    supported = " or ".join(TOKEI_VERSIONS)
    if shutil.which("tokei") is None:
        raise SystemExit(
            f"Error: tokei {supported} is required. Install it, then rerun "
            "uv run tools/check_complexity.py."
        )
    installed = _run(("tokei", "--version"))
    if installed.removeprefix("tokei ").partition(".")[0] not in TOKEI_VERSIONS:
        raise SystemExit(
            f"Error: tokei {supported} is required, but this is {installed.strip()}. "
            "Install a supported version, then rerun."
        )
    budget = tomllib.loads(BUDGET.read_text(encoding="utf-8"))
    labels, paths, units = budget["labels"], budget["paths"], budget["units"]
    missing = [path for group in paths.values() for path in group if not (ROOT / path).exists()]
    if missing:
        raise SystemExit(f"Error: missing complexity-budget paths: {', '.join(missing)}")
    limits = budget["code"] | budget["ruff"]
    measured = {
        "production": _code_lines(paths["production"]),
        "tests": _code_lines(paths["tests"]),
        "merge": _code_lines(paths["merge"]),
        "governed": _code_lines(paths["governed"]),
    }
    measured["total"] = measured["production"] + measured["tests"]
    modules = sorted(
        path
        for relative in paths["governed"]
        for path in (
            (ROOT / relative).rglob("*.py") if (ROOT / relative).is_dir() else (ROOT / relative,)
        )
    )
    module_lines = {
        path.relative_to(ROOT): _code_lines((str(path.relative_to(ROOT)),))
        for path in set(modules)
    }
    measured |= {
        "c901": _c901(("src/jj_stack",)),
        "governed_c901": _c901(paths["governed"]),
    }

    return _report(labels, limits, measured, module_lines, units)


if __name__ == "__main__":
    raise SystemExit(main())
