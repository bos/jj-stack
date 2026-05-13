from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.support.output_assertions import assert_output_contains

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "describe_with_prompt.py"
_SPEC = importlib.util.spec_from_file_location("describe_with_prompt", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
describe_with_prompt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(describe_with_prompt)


def test_prompt_line_uses_return_hint_instead_of_repeating_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", fake_input)

    value = describe_with_prompt.prompt_line("Title", "commit title", "commit title")

    assert value == "commit title"
    assert prompts == ["Title [return to use commit title]: "]


def test_prompt_body_accepts_return_to_use_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", fake_input)

    value = describe_with_prompt.prompt_body("commit body", "commit body")

    assert value == "commit body"
    assert prompts == ["Body [return to use commit body]: "]


def test_run_reports_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(describe_with_prompt, "main", interrupted)

    exit_code = describe_with_prompt.run()
    captured = capsys.readouterr()

    assert exit_code == 130
    assert not captured.out
    assert_output_contains(captured.err, "Interrupted.")
    assert "Traceback" not in captured.err
