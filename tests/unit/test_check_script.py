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
    (tests_dir / "test_fragile.py").write_text(
        "\n".join(
            [
                "def test_output(capsys) -> None:",
                "    captured = capsys.readouterr()",
                "    assert captured.out "
                "== ''",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)

    with pytest.raises(SystemExit, match="fragile test output assertions are not allowed"):
        check_script._check_fragile_test_output_assertions()
