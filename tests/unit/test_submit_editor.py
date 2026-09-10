"""The --edit pass: rendering and parsing the document, and running the editor."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jj_stack.commands.submit.editor import (
    _split_editor_command,
    edit_prs_in_editor,
    parse_description_edit_document,
    render_description_edit_document,
)
from jj_stack.commands.submit.models import GeneratedDescription
from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient
from tests.support.change_helpers import make_change


def _two_change_stack() -> tuple:
    bottom = make_change(
        commit_id="c1", change_id="bottomchange", description="feature 1\n\nBottom body.\n"
    )
    top = make_change(commit_id="c2", change_id="topchange", description="feature 2\n")
    return (bottom, top)


def test_edit_document_parse_rejects_unknown_change() -> None:
    changes = _two_change_stack()
    document = "====== change mysterychange\ntitle\n"

    with pytest.raises(CliError, match="unknown change"):
        parse_description_edit_document(document, changes=changes)


def test_edit_document_parse_rejects_repeated_change_section() -> None:
    changes = _two_change_stack()
    document = (
        "====== change topchange\nfeature 2\n"
        "====== change topchange\nfeature 2 again\n"
        "====== change bottomchange\nfeature 1\n"
    )

    with pytest.raises(CliError, match="repeat change"):
        parse_description_edit_document(document, changes=changes)


def test_edit_document_parse_rejects_section_without_title() -> None:
    changes = _two_change_stack()
    document = (
        "====== change topchange\nJJ: Draft: no\nfeature 2\n"
        "====== change bottomchange\nJJ: Draft: yes\n\n   \n"
    )

    with pytest.raises(CliError, match="no title line"):
        parse_description_edit_document(document, changes=changes)


def test_edit_document_parse_rejects_content_before_first_separator() -> None:
    changes = _two_change_stack()
    document = (
        "stray text\n====== change topchange\nfeature 2\n====== change bottomchange\nfeature 1\n"
    )

    with pytest.raises(CliError, match="before the first change separator"):
        parse_description_edit_document(document, changes=changes)


def test_edit_document_accepts_short_draft_states_case_insensitively() -> None:
    bottom, top = _two_change_stack()
    document = (
        f"====== change {top.change_id}\nJJ: Draft: y\nfeature 2\n"
        f"====== change {bottom.change_id}\nJJ: Draft: N\nfeature 1\n"
    )

    _, drafts = parse_description_edit_document(document, changes=(bottom, top))

    assert drafts == {bottom.change_id: False, top.change_id: True}


def test_edit_document_rejects_invalid_draft_state_for_named_change() -> None:
    change = _two_change_stack()[0]
    document = f"====== change {change.change_id}\nJJ: Draft: maybe\nfeature 1\n"

    with pytest.raises(CliError, match=f"{change.change_id[:8]}.*maybe.*expected yes or no"):
        parse_description_edit_document(document, changes=(change,))


def test_windows_editor_command_preserves_backslashes(monkeypatch) -> None:
    monkeypatch.setattr("jj_stack.commands.submit.editor.os.name", "nt")

    assert _split_editor_command(
        r"D:\a\jj-stack\jj-stack\.venv\Scripts\python.exe D:\tmp\editor.py"
    ) == [
        r"D:\a\jj-stack\jj-stack\.venv\Scripts\python.exe",
        r"D:\tmp\editor.py",
    ]
    assert _split_editor_command(
        r'"C:\Program Files\Python\python.exe" "D:\tmp\editor script.py"'
    ) == [
        r"C:\Program Files\Python\python.exe",
        r"D:\tmp\editor script.py",
    ]


def test_list_valued_jj_editor_config_is_split_as_arguments_not_one_filename() -> None:
    """`jj` accepts a list for `ui.editor` and `jj config get` prints it back as TOML text.

    Splitting that text as a shell word looked for an editor named `[code,--wait]`, so `--edit`
    was unusable for anyone configuring their editor that way.
    """

    assert _split_editor_command('["code","--wait"]') == ["code", "--wait"]
    assert _split_editor_command('[ "emacsclient", "-nw" ]') == ["emacsclient", "-nw"]
    # A path that merely contains brackets is still one filename.
    assert _split_editor_command("/usr/bin/editor[1]") == ["/usr/bin/editor[1]"]


def _isolate_editor_environment(monkeypatch, tmp_path: Path) -> None:
    jj_config = tmp_path / "jj-config.toml"
    jj_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("JJ_CONFIG", str(jj_config))
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


def test_edit_applies_editor_output_to_descriptions(monkeypatch, tmp_path: Path) -> None:
    _isolate_editor_environment(monkeypatch, tmp_path)
    editor = tmp_path / "editor.py"
    editor.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "",
                "path = Path(sys.argv[-1])",
                "text = path.read_text(encoding='utf-8')",
                "path.write_text(",
                "    text.replace('feature 2', 'feature 2 [edited]'),",
                "    encoding='utf-8',",
                ")",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")

    descriptions, drafts, document_path = edit_prs_in_editor(
        descriptions={
            "bottomchange": GeneratedDescription(body="Bottom body.", title="feature 1"),
            "topchange": GeneratedDescription(body="", title="feature 2"),
        },
        drafts={"bottomchange": True, "topchange": False},
        jj_client=JjClient(tmp_path),
        changes=_two_change_stack(),
    )

    assert drafts == {"bottomchange": True, "topchange": False}
    assert descriptions["topchange"].title == "feature 2 [edited]"
    assert descriptions["bottomchange"].title == "feature 1"
    assert descriptions["bottomchange"].body == "Bottom body."
    assert document_path.is_file()
    document_path.unlink()


def test_edit_aborts_when_editor_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    _isolate_editor_environment(monkeypatch, tmp_path)
    editor = tmp_path / "editor.py"
    editor.write_text("raise SystemExit(3)\n", encoding="utf-8")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")
    document_path = tmp_path / "saved-edit.md"
    document_path.write_text(
        render_description_edit_document(
            descriptions={
                "bottomchange": GeneratedDescription(body="Bottom body.", title="feature 1"),
                "topchange": GeneratedDescription(body="", title="feature 2"),
            },
            drafts={"bottomchange": False, "topchange": False},
            changes=_two_change_stack(),
        ),
        encoding="utf-8",
    )

    with pytest.raises(CliError, match="exited with status 3") as caught:
        edit_prs_in_editor(
            descriptions={
                "bottomchange": GeneratedDescription(body="Bottom body.", title="feature 1"),
                "topchange": GeneratedDescription(body="", title="feature 2"),
            },
            drafts={"bottomchange": False, "topchange": False},
            jj_client=JjClient(tmp_path),
            changes=_two_change_stack(),
            document_path=document_path,
        )

    assert f"--resume-edit {document_path}" in str(caught.value)
