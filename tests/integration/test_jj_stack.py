from __future__ import annotations

from pathlib import Path

import pytest

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


def test_list_git_remotes_preserves_distinct_fetch_and_push_urls(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_command(
        [
            "jj",
            "git",
            "remote",
            "add",
            "origin",
            "https://github.test/octo-org/stacked-review.git",
            "--push-url",
            "git@github.test:octo-org/stacked-review.git",
        ],
        repo,
    )

    (remote,) = JjClient(repo).list_git_remotes()
    assert remote.name == "origin"
    assert remote.fetch_url == "https://github.test/octo-org/stacked-review.git"
    assert remote.push_url == "git@github.test:octo-org/stacked-review.git"


@pytest.mark.parametrize("layout_flag", ("--colocate", "--no-colocate"))
def test_direct_git_review_ref_operations_use_the_backing_store(
    tmp_path: Path,
    layout_flag: str,
) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    run_command(["git", "init", "--bare", str(remote)], tmp_path)
    run_command(["jj", "git", "init", layout_flag, str(repo)], tmp_path)
    commit_file(repo, "base", "base.txt")
    commit_file(repo, "feature", "feature.txt")
    old_commit = _commit_id(repo, "@--")
    new_commit = _commit_id(repo, "@-")
    run_command(["jj", "git", "remote", "add", "origin", str(remote)], repo)
    run_command(["jj", "bookmark", "create", "seed", "-r", "@--"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "seed"], repo)
    branch = "review/foundation"
    run_command(
        [
            "git",
            "--git-dir",
            str(remote),
            "update-ref",
            f"refs/heads/{branch}",
            old_commit,
        ],
        repo,
    )
    client = JjClient(repo)

    assert client.list_remote_branches(
        remote="origin",
        patterns=(f"refs/heads/{branch}",),
    ) == {branch: old_commit}
    client.update_untracked_remote_bookmark(
        remote="origin",
        bookmark=branch,
        desired_target=new_commit,
        expected_remote_target=old_commit,
    )
    assert client.list_remote_branches(
        remote="origin",
        patterns=(f"refs/heads/{branch}",),
    ) == {branch: new_commit}

    client.delete_remote_bookmarks(
        remote="origin",
        deletions=((branch, new_commit),),
        fetch=False,
    )
    assert client.list_remote_branches(
        remote="origin",
        patterns=(f"refs/heads/{branch}",),
    ) == {}
    git_root = Path(run_command(["jj", "git", "root"], repo).stdout.strip())
    assert (git_root == repo / ".git") is (layout_flag == "--colocate")


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


def _commit_id(repo: Path, revset: str) -> str:
    return run_command(
        ["jj", "log", "--no-graph", "-r", revset, "-T", "commit_id"],
        repo,
    ).stdout.strip()
