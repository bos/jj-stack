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


def test_complexity_collection_ignores_property_and_pytest_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-qq")
    monkeypatch.setenv("JJ_STACK_SUBMIT_PROPERTY_SCENARIOS", "99")
    monkeypatch.setenv("UNRELATED_SETTING", "kept")

    environment = complexity_script._collection_env()

    assert "PYTEST_ADDOPTS" not in environment
    assert "JJ_STACK_SUBMIT_PROPERTY_SCENARIOS" not in environment
    assert environment["UNRELATED_SETTING"] == "kept"


def test_complexity_report_explains_numbers_and_owns_its_exit_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    labels = {
        "production": "Production",
        "tests": "Tests",
        "total": "Production and tests combined",
        "checker": "Complexity checker",
        "land": "Merge command",
        "governed": "Landing and recovery code",
        "c901": "Production",
        "governed_c901": "Landing and recovery code",
        "fixed_property": "Property-test cases",
        "landing_recovery": "Landing and recovery cases",
    }
    limits = {name: 10 for name in labels} | {"governed_module": 5}
    limits["governed_c901"] = 0
    measured = {name: 8 for name in labels}
    measured |= {"checker": 10, "land": 12, "governed": 11, "governed_c901": 0}
    units = {
        name: "line" for name in ("production", "tests", "total", "checker", "land", "governed")
    }
    units |= {"c901": "function", "governed_c901": "function"}
    units |= {"fixed_property": "case", "landing_recovery": "case"}

    exit_code = complexity_script._report(
        labels,
        limits,
        measured,
        {Path("safe.py"): 4, Path("over.py"): 6, Path("highest.py"): 7},
        units,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Code size" in captured.out
    assert "Production: 8 lines (limit 10 lines; 2 lines available)" in captured.out
    assert "Complexity checker: 10 lines (limit 10 lines; at limit)" in captured.out
    assert "Merge command: 12 lines (limit 10 lines; OVER LIMIT by 2 lines)" in captured.out
    assert "Functions with a complexity score above 10" in captured.out
    assert (
        "Landing and recovery code: 0 functions (limit 0 functions; requirement met)"
        in captured.out
    )
    assert "Fixed test-case limits" in captured.out
    assert "Landing/recovery file sizes (3 files; 5-line limit each)" in captured.out
    assert "Over limit:" in captured.out
    assert "highest.py: 7 lines (limit 5 lines; OVER LIMIT by 2 lines)" in captured.out
    assert "over.py: 6 lines (limit 5 lines; OVER LIMIT by 1 line)" in captured.out
    assert "safe.py" not in captured.out
    assert "Margin is limit minus measured" not in captured.out
    assert "quality scores" not in captured.out
    assert "Result: failed" in captured.err
    assert "highest.py: OVER LIMIT by 2 lines" in captured.err
    assert "over.py: OVER LIMIT by 1 line" in captured.err
    assert captured.err.index("Merge command") < captured.err.index("Landing and recovery code")
    assert captured.err.index("highest.py") < captured.err.index("over.py")

    passing_measured = {name: min(value, limits[name]) for name, value in measured.items()}
    passing_exit_code = complexity_script._report(
        labels,
        limits,
        passing_measured,
        {
            Path("hidden.py"): 1,
            Path("z-tie.py"): 3,
            Path("second.py"): 3,
            Path("near.py"): 4,
            Path("fourth.py"): 2,
        },
        units,
    )
    passing = capsys.readouterr()

    assert passing_exit_code == 0
    assert "Closest to the limit:" in passing.out
    assert "near.py" in passing.out
    assert "second.py" in passing.out
    assert "z-tie.py" in passing.out
    assert passing.out.index("near.py") < passing.out.index("second.py")
    assert passing.out.index("second.py") < passing.out.index("z-tie.py")
    assert "fourth.py" not in passing.out
    assert "hidden.py" not in passing.out
    assert "Result: all 15 limits passed" in passing.out
    assert passing.err == ""
