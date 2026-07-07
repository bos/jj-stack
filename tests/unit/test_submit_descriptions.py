"""Pull request description resolution: template fallback and the --edit editor pass."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jj_stack.commands.submit.descriptions import (
    parse_description_edit_document,
    render_description_edit_document,
    resolve_generated_descriptions,
)
from jj_stack.commands.submit.models import GeneratedDescription
from jj_stack.errors import CliError, UsageError
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

    body = _resolve_default_bodies(
        tmp_path, description="fix: subject\n\nReal body paragraph.\n"
    )

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


def test_edit_document_round_trips_titles_and_bodies() -> None:
    revisions = _two_change_stack()
    descriptions = {
        "bottomchange": GeneratedDescription(body="Bottom body.", title="feature 1"),
        "topchange": GeneratedDescription(body="", title="feature 2"),
    }

    document = render_description_edit_document(
        descriptions=descriptions, revisions=revisions
    )
    parsed = parse_description_edit_document(document, revisions=revisions)

    assert parsed == descriptions
    # The head change renders first, matching how view presents a stack.
    assert document.index("topchange") < document.index("bottomchange")


def test_edit_document_parse_rejects_unknown_change() -> None:
    revisions = _two_change_stack()
    document = "====== change mysterychange\ntitle\n"

    with pytest.raises(CliError, match="unknown change"):
        parse_description_edit_document(document, revisions=revisions)


def test_edit_document_parse_rejects_missing_change_section() -> None:
    revisions = _two_change_stack()
    document = "====== change topchange\nfeature 2\n"

    with pytest.raises(CliError, match="missing change"):
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
        "====== change topchange\nfeature 2\n"
        "====== change bottomchange\n\n   \n"
    )

    with pytest.raises(CliError, match="no title line"):
        parse_description_edit_document(document, revisions=revisions)


def test_edit_document_parse_rejects_content_before_first_separator() -> None:
    revisions = _two_change_stack()
    document = (
        "stray text\n"
        "====== change topchange\nfeature 2\n"
        "====== change bottomchange\nfeature 1\n"
    )

    with pytest.raises(CliError, match="before the first change separator"):
        parse_description_edit_document(document, revisions=revisions)


def test_edit_is_mutually_exclusive_with_describe_with(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="--edit.*--describe-with"):
        resolve_generated_descriptions(
            descriptions=(),
            describe_with="helper",
            edit=True,
            jj_client=JjClient(tmp_path),
            revisions=_two_change_stack(),
            selected_revset="@-",
        )


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
        edit=True,
        jj_client=JjClient(tmp_path),
        revisions=_two_change_stack(),
        selected_revset="@-",
    )

    assert stack_description is None
    assert descriptions["topchange"].title == "feature 2 [edited]"
    assert descriptions["bottomchange"].title == "feature 1"
    assert descriptions["bottomchange"].body == "Bottom body."


def test_edit_aborts_when_editor_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    _isolate_editor_environment(monkeypatch, tmp_path)
    editor = tmp_path / "editor.py"
    editor.write_text("raise SystemExit(3)\n", encoding="utf-8")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")

    with pytest.raises(CliError, match="exited with status 3"):
        resolve_generated_descriptions(
            descriptions=(),
            describe_with=None,
            edit=True,
            jj_client=JjClient(tmp_path),
            revisions=_two_change_stack(),
            selected_revset="@-",
        )

