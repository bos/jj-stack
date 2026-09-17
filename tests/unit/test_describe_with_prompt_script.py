from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "describe_with_prompt.py"
_SPEC = importlib.util.spec_from_file_location("describe_with_prompt", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
describe_with_prompt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(describe_with_prompt)


def test_accepting_defaults_preserves_title_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    assert describe_with_prompt.prompt_title("commit title", "commit title") == "commit title"
    assert describe_with_prompt.prompt_body("commit body", "commit body") == "commit body"
