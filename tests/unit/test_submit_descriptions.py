"""Pull request description resolution: template fallback and the --edit editor pass."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jj_stack.commands.submit.descriptions import (
    _split_editor_command,
    edit_pull_requests_in_editor,
    parse_description_edit_document,
    render_description_edit_document,
    resolve_generated_descriptions,
)
from jj_stack.commands.submit.models import GeneratedDescription
from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient
from tests.support.revision_helpers import make_revision


def _resolve_default_bodies(tmp_path: Path, *, description: str) -> str:
    revision = make_revision(commit_id="c1", change_id="ch1", description=description)
    descriptions, stack_description = resolve_generated_descriptions(
        descriptions=(),
        describe_with=None,
        jj_client=JjClient(tmp_path),
        revisions=(revision,),
        selected_revset="@-",
    )
    assert stack_description is None
    return descriptions["ch1"].body


def test_bodyless_change_uses_pull_request_template(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github"
    template_dir.mkdir()
    (template_dir / "PULL_REQUEST_TEMPLATE.md").write_text(
        "## Summary\n\n## Testing\n", encoding="utf-8"
    )

    body = _resolve_default_bodies(tmp_path, description="fix: one-line subject\n")

    assert body == "## Summary\n\n## Testing"


def test_change_description_body_wins_over_pull_request_template(tmp_path: Path) -> None:
    (tmp_path / "PULL_REQUEST_TEMPLATE.md").write_text("## Template\n", encoding="utf-8")

    body = _resolve_default_bodies(tmp_path, description="fix: subject\n\nReal body paragraph.\n")

    assert body == "Real body paragraph."


def test_bodyless_change_falls_back_to_subject_without_template(tmp_path: Path) -> None:
    body = _resolve_default_bodies(tmp_path, description="fix: subject only\n")

    assert body == "fix: subject only"


def test_empty_pull_request_template_counts_as_absent(tmp_path: Path) -> None:
    (tmp_path / "PULL_REQUEST_TEMPLATE.md").write_text("  \n\n", encoding="utf-8")

    body = _resolve_default_bodies(tmp_path, description="fix: subject only\n")

    assert body == "fix: subject only"


def test_pull_request_template_prefers_github_directory_over_root(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github"
    template_dir.mkdir()
    (template_dir / "PULL_REQUEST_TEMPLATE.md").write_text("github dir", encoding="utf-8")
    (tmp_path / "PULL_REQUEST_TEMPLATE.md").write_text("repo root", encoding="utf-8")

    body = _resolve_default_bodies(tmp_path, description="fix: subject\n")

    assert body == "github dir"


def _two_change_stack() -> tuple:
    bottom = make_revision(
        commit_id="c1", change_id="bottomchange", description="feature 1\n\nBottom body.\n"
    )
    top = make_revision(commit_id="c2", change_id="topchange", description="feature 2\n")
    return (bottom, top)


def test_edit_document_round_trips_titles_bodies_and_draft_states() -> None:
    revisions = _two_change_stack()
    descriptions = {
        "bottomchange": GeneratedDescription(body="Bottom body.", title="feature 1"),
        "topchange": GeneratedDescription(body="", title="feature 2"),
    }

    drafts = {"bottomchange": True, "topchange": False}

    document = render_description_edit_document(
        descriptions=descriptions,
        drafts=drafts,
        revisions=revisions,
    )
    parsed_descriptions, parsed_drafts = parse_description_edit_document(
        document,
        revisions=revisions,
    )

    assert parsed_descriptions == descriptions
    assert parsed_drafts == drafts
    # The head change renders first, matching how view presents a stack.
    assert document.index("topchange") < document.index("bottomchange")


def test_edit_document_parse_rejects_unknown_change() -> None:
    revisions = _two_change_stack()
    document = "====== change mysterychange\ntitle\n"

    with pytest.raises(CliError, match="unknown change"):
        parse_description_edit_document(document, revisions=revisions)


def test_edit_document_parse_rejects_repeated_change_section() -> None:
    revisions = _two_change_stack()
    document = (
        "====== change topchange\nfeature 2\n"
        "====== change topchange\nfeature 2 again\n"
        "====== change bottomchange\nfeature 1\n"
    )

    with pytest.raises(CliError, match="repeat change"):
        parse_description_edit_document(document, revisions=revisions)


def test_edit_document_parse_rejects_section_without_title() -> None:
    revisions = _two_change_stack()
    document = (
        "====== change topchange\nJJ: Draft: no\nfeature 2\n"
        "====== change bottomchange\nJJ: Draft: yes\n\n   \n"
    )

    with pytest.raises(CliError, match="no title line"):
        parse_description_edit_document(document, revisions=revisions)


def test_edit_document_parse_rejects_content_before_first_separator() -> None:
    revisions = _two_change_stack()
    document = (
        "stray text\n====== change topchange\nfeature 2\n====== change bottomchange\nfeature 1\n"
    )

    with pytest.raises(CliError, match="before the first change separator"):
        parse_description_edit_document(document, revisions=revisions)


def test_edit_document_accepts_short_draft_states_case_insensitively() -> None:
    bottom, top = _two_change_stack()
    document = (
        f"====== change {top.change_id}\nJJ: Draft: y\nfeature 2\n"
        f"====== change {bottom.change_id}\nJJ: Draft: N\nfeature 1\n"
    )

    _, drafts = parse_description_edit_document(document, revisions=(bottom, top))

    assert drafts == {bottom.change_id: False, top.change_id: True}


def test_edit_document_rejects_invalid_draft_state_for_named_change() -> None:
    revision = _two_change_stack()[0]
    document = f"====== change {revision.change_id}\nJJ: Draft: maybe\nfeature 1\n"

    with pytest.raises(CliError, match=f"{revision.change_id[:8]}.*maybe.*expected yes or no"):
        parse_description_edit_document(document, revisions=(revision,))


def test_windows_editor_command_preserves_backslashes(monkeypatch) -> None:
    monkeypatch.setattr("jj_stack.commands.submit.descriptions.os.name", "nt")

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

    descriptions, stack_description = resolve_generated_descriptions(
        descriptions=(),
        describe_with=None,
        jj_client=JjClient(tmp_path),
        revisions=_two_change_stack(),
        selected_revset="@-",
    )
    descriptions, drafts = edit_pull_requests_in_editor(
        descriptions=descriptions,
        drafts={"bottomchange": True, "topchange": False},
        jj_client=JjClient(tmp_path),
        revisions=_two_change_stack(),
    )

    assert stack_description is None
    assert drafts == {"bottomchange": True, "topchange": False}
    assert descriptions["topchange"].title == "feature 2 [edited]"
    assert descriptions["bottomchange"].title == "feature 1"
    assert descriptions["bottomchange"].body == "Bottom body."


def test_edit_aborts_when_editor_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    _isolate_editor_environment(monkeypatch, tmp_path)
    editor = tmp_path / "editor.py"
    editor.write_text("raise SystemExit(3)\n", encoding="utf-8")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")

    with pytest.raises(CliError, match="exited with status 3"):
        edit_pull_requests_in_editor(
            descriptions={
                "bottomchange": GeneratedDescription(body="Bottom body.", title="feature 1"),
                "topchange": GeneratedDescription(body="", title="feature 2"),
            },
            drafts={"bottomchange": False, "topchange": False},
            jj_client=JjClient(tmp_path),
            revisions=_two_change_stack(),
        )
