"""Default pull request body resolution, including the pull request template fallback."""

from __future__ import annotations

from pathlib import Path

from jj_stack.commands.submit.descriptions import resolve_generated_descriptions
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
