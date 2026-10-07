from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CHECK_PATH = Path(__file__).resolve().parents[2] / "check.py"
_SPEC = importlib.util.spec_from_file_location("jj_stack_check", _CHECK_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
check_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_script)

_COMPLEXITY_PATH = Path(__file__).resolve().parents[2] / "tools" / "check_complexity.py"
_COMPLEXITY_SPEC = importlib.util.spec_from_file_location(
    "jj_stack_check_complexity", _COMPLEXITY_PATH
)
assert _COMPLEXITY_SPEC is not None
assert _COMPLEXITY_SPEC.loader is not None
complexity_script = importlib.util.module_from_spec(_COMPLEXITY_SPEC)
_COMPLEXITY_SPEC.loader.exec_module(complexity_script)


def test_fragile_test_output_check_accepts_clean_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_clean.py").write_text(
        "\n".join(
            [
                "from tests.support.output_assertions import assert_output_contains",
                "",
                "def test_output() -> None:",
                "    assert_output_contains('wrapped output', 'wrapped output')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)

    check_script._check_fragile_test_output_assertions()


def test_fragile_test_output_check_rejects_exact_captured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    fragile_assertion = "".join(("    assert captured.out ", "== ''"))
    (tests_dir / "test_fragile.py").write_text(
        "\n".join(
            [
                "def test_output(capsys) -> None:",
                "    captured = capsys.readouterr()",
                fragile_assertion,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)

    with pytest.raises(SystemExit, match="fragile test output assertions are not allowed"):
        check_script._check_fragile_test_output_assertions()


def test_complexity_report_rejects_exceeded_limits_and_accepts_the_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels = {
        "production": "Production",
        "tests": "Tests",
        "total": "Production and tests combined",
        "merge": "Merge command",
        "governed": "Merge and recovery code",
        "c901": "Production",
        "governed_c901": "Merge and recovery code",
    }
    limits = {name: 10 for name in labels} | {"governed_module": 5}
    limits["governed_c901"] = 0
    measured = {name: 8 for name in labels}
    measured |= {"tests": 10, "merge": 12, "governed": 11, "governed_c901": 0}
    units = {name: "line" for name in ("production", "tests", "total", "merge", "governed")}
    units |= {"c901": "function", "governed_c901": "function"}

    exit_code = complexity_script._report(
        labels,
        limits,
        measured,
        {Path("safe.py"): 4, Path("over.py"): 6, Path("highest.py"): 7},
        units,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Production: 8 lines (limit 10 lines; 2 lines available)" in captured.out
    assert "Tests: 10 lines (limit 10 lines; 0 lines available)" in captured.out
    assert "Merge command: 12 lines (limit 10 lines; OVER LIMIT by 2 lines)" in captured.out
    assert (
        "Merge and recovery code: 0 functions (limit 0 functions; requirement met)"
        in captured.out
    )
    assert "highest.py: 7 lines (limit 5 lines; OVER LIMIT by 2 lines)" in captured.out
    assert "over.py: 6 lines (limit 5 lines; OVER LIMIT by 1 line)" in captured.out
    assert "safe.py" not in captured.out
    assert "Result: failed" in captured.err
    assert "highest.py: OVER LIMIT by 2 lines" in captured.err
    assert "over.py: OVER LIMIT by 1 line" in captured.err

    passing_measured = {name: min(value, limits[name]) for name, value in measured.items()}
    passing_exit_code = complexity_script._report(
        labels,
        limits,
        passing_measured,
        {Path("boundary.py"): limits["governed_module"]},
        units,
    )
    passing = capsys.readouterr()

    assert passing_exit_code == 0
    assert passing.err == ""


def test_docstring_lines_are_counted_apart_from_code(tmp_path: Path, monkeypatch) -> None:
    """Docstrings must not consume a code budget, and an apostrophe must not change the count."""

    monkeypatch.setattr(complexity_script, "ROOT", tmp_path)
    plain = tmp_path / "plain.py"
    plain.write_text('def f() -> None:\n    """One.\n\n    Two.\n    """\n\n    x = 1\n')
    apostrophe = tmp_path / "apostrophe.py"
    apostrophe.write_text('def f() -> None:\n    """A stack\'s docstring."""\n\n    x = 1\n')
    no_docstring = tmp_path / "bare.py"
    no_docstring.write_text("def f() -> None:\n    x = 1\n")

    assert complexity_script._docstring_lines(Path("plain.py")) == 4
    assert complexity_script._docstring_lines(Path("apostrophe.py")) == 1
    assert complexity_script._docstring_lines(Path("bare.py")) == 0
