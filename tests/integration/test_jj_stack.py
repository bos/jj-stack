from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.jj.client import JjClient, UnsupportedStackError

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
    _new_child(repo, feature_1)
    commit_file(repo, "feature side", "feature-side.txt")

    stack = JjClient(repo).discover_review_stack(feature_2)

    assert [revision.subject for revision in stack.revisions] == ["feature 1", "feature 2"]


def test_discover_review_stack_fails_with_root_before_trunk(tmp_path: Path) -> None:
    # If a commit's ancestry reaches the root before finding trunk(), the
    # walk should fail with a targeted UnsupportedStackError rather than
    # silently producing a corrupted stack.  We set trunk() to a bookmark
    # that is NOT an ancestor of the selected head.
    repo = init_repo(tmp_path, configure_trunk=False)
    # Create a unlinked bookmark that lives on @- (the base commit).
    run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    # Create a sibling branch starting from the root commit, not from main.
    run_command(["jj", "new", "root()"], repo)
    run_command(["jj", "bookmark", "create", "trunk-alias", "-r", "@"], repo)
    run_command(
        ["jj", "config", "set", "--repo", 'revset-aliases."trunk()"', "trunk-alias"],
        repo,
    )
    # Now go back and add a commit on the main branch.
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature on main", "feature.txt")
    head = _current_parent_commit_id(repo)

    with pytest.raises(
        UnsupportedStackError,
        match="selected-parent path reached the root commit before trunk\\(\\)",
    ):
        JjClient(repo).discover_review_stack(head)


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


def _new_child(repo: Path, parent_commit_id: str) -> None:
    run_command(["jj", "new", parent_commit_id], repo)
