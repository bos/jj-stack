from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.support.output_assertions import assert_output_contains

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "describe_with_editor.py"
_SPEC = importlib.util.spec_from_file_location("describe_with_editor", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
describe_with_editor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(describe_with_editor)


def test_initial_editor_text_uses_readable_helper_comment_block() -> None:
    text = describe_with_editor.initial_editor_text(
        context_lines=["commit title", "", "commit body"],
        mode="pr",
        revset="abc",
    )

    assert "<!-- jj-stack:\n" in text
    assert "Commit description for abc:\ncommit title\n\ncommit body\n-->" in text


def test_parse_edited_description_ignores_helper_comment_blocks() -> None:
    parsed = describe_with_editor.parse_edited_description(
        "\n".join(
            [
                "<!-- jj-stack:",
                "commit context",
                "-->",
                "",
                "# Markdown title",
                "",
                "Body with **formatting**.",
                "<!-- jj-stack: more context -->",
            ]
        )
    )

    assert parsed == ("# Markdown title", "Body with **formatting**.")


def test_parse_edited_description_returns_none_when_only_comments_remain() -> None:
    parsed = describe_with_editor.parse_edited_description(
        "\n".join(
            [
                "",
                "<!-- jj-stack:",
                "write a title",
                "",
                "commit context",
                "-->",
                "",
            ]
        )
    )

    assert parsed is None


def test_main_emits_json_from_editor_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    editor = tmp_path / "editor.py"
    editor.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "",
                "Path(sys.argv[-1]).write_text(",
                "    'Edited title\\n\\nEdited body\\n<!-- jj-stack:\\ncontext\\n-->\\n',",
                "    encoding='utf-8',",
                ")",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")
    monkeypatch.setattr(
        describe_with_editor,
        "run_jj",
        lambda *args: "commit title\n\ncommit body\n",
    )

    exit_code = describe_with_editor.main(["--pr", "abc"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out) == {
        "body": "Edited body",
        "title": "Edited title",
    }


def test_run_reports_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted() -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(describe_with_editor, "main", interrupted)

    exit_code = describe_with_editor.run()
    captured = capsys.readouterr()

    assert exit_code == 130
    assert not captured.out
    assert_output_contains(captured.err, "Interrupted.")
    assert "Traceback" not in captured.err
