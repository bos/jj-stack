from __future__ import annotations

from pathlib import Path

from jj_stack.jj.client import JjClient

from ..support.integration_helpers import (
    commit_file,
    init_repo,
    run_command,
)


def test_discover_review_stack_walks_linear_history_from_default_head(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()

    assert stack.selected_revset == "@-"
    assert [revision.subject for revision in stack.revisions] == ["feature 1", "feature 2"]


def test_discover_review_stack_ignores_off_path_reviewable_child(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    feature_1 = _current_parent_commit_id(repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    feature_2 = _current_parent_commit_id(repo)
    run_command(["jj", "new", feature_1], repo)
    commit_file(repo, "feature side", "feature-side.txt")

    stack = JjClient(repo).discover_review_stack(feature_2)

    assert [revision.subject for revision in stack.revisions] == ["feature 1", "feature 2"]


def _current_parent_commit_id(repo: Path) -> str:
    completed = run_command(
        [
            "jj",
            "log",
            "--no-graph",
            "-r",
            "@-",
            "-T",
            "commit_id",
        ],
        repo,
    )
    return completed.stdout.strip()

