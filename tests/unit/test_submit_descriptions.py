"""Pull request description resolution: template fallback and refresh comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.commands.submit.descriptions import (
    preserve_external_pr_text,
    read_pr_template,
    resolve_generated_descriptions,
)
from jj_stack.commands.submit.models import GeneratedDescription
from jj_stack.errors import UsageError
from jj_stack.identifiers import ChangeId
from jj_stack.jj.client import JjClient
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead
from tests.support.change_helpers import make_change


def _resolve_default_bodies(tmp_path: Path, *, description: str) -> str:
    change = make_change(commit_id="c1", change_id="ch1", description=description)
    descriptions, stack_description = resolve_generated_descriptions(
        descriptions=(),
        describe_with=None,
        jj_client=JjClient(tmp_path),
        changes=(change,),
        selected_revset="@-",
        template=read_pr_template(tmp_path) or "",
    )
    assert stack_description is None
    return descriptions[ChangeId("ch1")].body


def test_bodyless_change_prefers_github_pr_template_over_root(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github"
    template_dir.mkdir()
    (template_dir / "PULL_REQUEST_TEMPLATE.md").write_text(
        "## Summary\n\n## Testing\n", encoding="utf-8"
    )
    (tmp_path / "PULL_REQUEST_TEMPLATE.md").write_text("Root template\n", encoding="utf-8")

    body = _resolve_default_bodies(tmp_path, description="fix: one-line subject\n")

    assert body == "## Summary\n\n## Testing"


def test_change_description_body_wins_over_pr_template(tmp_path: Path) -> None:
    (tmp_path / "PULL_REQUEST_TEMPLATE.md").write_text("## Template\n", encoding="utf-8")

    body = _resolve_default_bodies(tmp_path, description="fix: subject\n\nReal body paragraph.\n")

    assert body == "Real body paragraph."


def test_empty_pr_template_repeats_the_subject(tmp_path: Path) -> None:
    (tmp_path / "PULL_REQUEST_TEMPLATE.md").write_text("  \n\n", encoding="utf-8")

    body = _resolve_default_bodies(tmp_path, description="fix: subject only\n")

    assert body == "fix: subject only"


def test_stack_description_requires_more_than_one_change(tmp_path: Path) -> None:
    change = make_change(commit_id="c1", change_id="ch1", description="fix: subject\n")

    with pytest.raises(UsageError, match="more than one change"):
        resolve_generated_descriptions(
            descriptions=("stack=body.md",),
            describe_with=None,
            jj_client=JjClient(tmp_path),
            changes=(change,),
            selected_revset="@-",
            template="",
        )


def _live_pr(*, body: str, title: str) -> GithubPR:
    branch = "jj-stack/feature-ch1"
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        body=body,
        head=GithubPRHead(label=f"octo-org:{branch}", ref=branch, sha="head-commit"),
        html_url="https://github.test/octo-org/repo/pull/1",
        node_id="PR_1",
        number=1,
        state="open",
        title=title,
    )


def test_refresh_check_compares_the_body_submit_wrote_not_the_raw_description() -> None:
    """Unfolded and template bodies are submit's own text; keeping the wrapping is an edit."""

    wrapped_body = "A paragraph that\nwraps across two source lines."
    submitted = {
        ChangeId("ch1"): frozenset((f"feature 1\n\n{wrapped_body}\n",)),
        ChangeId("ch2"): frozenset(("feature 2\n",)),
        ChangeId("ch3"): frozenset((f"feature 3\n\n{wrapped_body}\n",)),
    }

    preserved = preserve_external_pr_text(
        descriptions={
            change_id: GeneratedDescription(
                body=f"fresh body {change_id}",
                title=f"fresh title {change_id}",
            )
            for change_id in submitted
        },
        prs={
            ChangeId("ch1"): _live_pr(
                body="A paragraph that wraps across two source lines.",
                title="feature 1",
            ),
            ChangeId("ch2"): _live_pr(body="## Checklist", title="feature 2"),
            ChangeId("ch3"): _live_pr(body=wrapped_body, title="feature 3"),
        },
        submitted_descriptions=submitted,
        template="## Checklist",
    )

    for change_id in (ChangeId("ch1"), ChangeId("ch2")):
        assert preserved[change_id] == GeneratedDescription(
            body=f"fresh body {change_id}",
            title=f"fresh title {change_id}",
        )
    assert preserved[ChangeId("ch3")] == GeneratedDescription(
        body=wrapped_body, title="feature 3"
    )


def test_a_submitted_commit_the_repo_never_held_is_not_an_edit_on_github() -> None:
    """`relink --replace-remote` can save a remote commit that never reaches local history.

    Submit cannot rebuild the text that commit implies, and preserving the pair would freeze it
    for the rest of the pull request's life.
    """

    preserved = preserve_external_pr_text(
        descriptions={
            ChangeId("ch1"): GeneratedDescription(body="edited body", title="feature 1")
        },
        prs={ChangeId("ch1"): _live_pr(body="feature 1", title="feature 1")},
        submitted_descriptions={},
        template="",
    )

    assert preserved[ChangeId("ch1")] == GeneratedDescription(
        body="edited body", title="feature 1"
    )
