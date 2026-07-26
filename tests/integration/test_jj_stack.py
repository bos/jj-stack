from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient, ReviewRefUpdate
from jj_stack.ui import plain_text

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


@pytest.mark.parametrize(
    ("layout_flag", "exposure"),
    (("--no-colocate", "import"), ("--colocate", "fetch")),
)
def test_stale_raw_review_ref_is_rejected_after_broad_operations(
    tmp_path: Path,
    layout_flag: str,
    exposure: str,
) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    run_command(["git", "init", "--bare", str(remote)], tmp_path)
    run_command(["jj", "git", "init", layout_flag, str(repo)], tmp_path)
    commit_file(repo, "feature", "feature.txt")
    commit_id = _commit_id(repo, "@-")
    change_id = _change_id(repo, "@-")
    branch = f"review/feature-{change_id[:8]}"
    run_command(["jj", "git", "remote", "add", "origin", str(remote)], repo)
    run_command(["jj", "bookmark", "create", branch, "-r", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", branch], repo)
    run_command(["jj", "bookmark", "forget", branch], repo)
    run_command(["jj", "bookmark", "forget", "--include-remotes", branch], repo)
    run_command(["jj", "git", "export"], repo)
    client = JjClient(repo)
    client.ensure_review_fetch_isolation(remote="origin")

    git_root = Path(run_command(["jj", "git", "root"], repo).stdout.strip())
    run_command(
        [
            "git",
            "--git-dir",
            str(git_root),
            "update-ref",
            f"refs/remotes/origin/{branch}",
            commit_id,
        ],
        repo,
    )
    assert client.list_imported_review_bookmarks() == ()

    with pytest.raises(CliError) as exposed_raised:
        if exposure == "fetch":
            client.fetch_remote(remote="origin")
        else:
            with client.import_remote_review_ref(
                remote="origin",
                branch=branch,
                expected_target=commit_id,
                expected_change_id=change_id,
            ):
                pytest.fail("an imported stale review ref should prevent attachment")

    assert exposed_raised.value.hint is not None
    imported_hint = plain_text(exposed_raised.value.hint)
    forget = imported_hint.split("run ", maxsplit=1)[1].split(", then run ", maxsplit=1)[0]
    export = imported_hint.split(", then run ", maxsplit=1)[1].split(".", maxsplit=1)[0]
    assert client.list_imported_review_bookmarks() == (branch,)
    assert client.review_temp_artifacts().bookmark_targets == ()
    assert client.review_temp_artifacts().ref_target is None
    run_command(shlex.split(forget), repo)
    run_command(shlex.split(export), repo)
    assert client.list_imported_review_bookmarks() == ()

    client.fetch_remote(remote="origin")
    with client.import_remote_review_ref(
        remote="origin",
        branch=branch,
        expected_target=commit_id,
        expected_change_id=change_id,
    ) as imported:
        assert imported.commit_id == commit_id
    assert client.list_imported_review_bookmarks() == ()
    assert client.review_temp_artifacts().bookmark_targets == ()
    assert client.review_temp_artifacts().ref_target is None


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
    new_change_id = _change_id(repo, "@-")
    run_command(["jj", "git", "remote", "add", "origin", str(remote)], repo)
    run_command(["jj", "bookmark", "create", "seed", "-r", "@--"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "seed"], repo)
    old_change_id = _change_id(repo, "@--")
    branch = f"review/foundation-{old_change_id[:8]}"
    created_branch = f"review/created-{new_change_id[:8]}"
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

    client.fetch_remote(remote="origin")
    assert client.list_imported_review_bookmarks() == ()
    assert client.list_remote_branches(
        remote="origin",
        patterns=(f"refs/heads/{branch}",),
    ) == {branch: old_commit}

    publisher = tmp_path / "publisher"
    run_command(["jj", "git", "init", "--no-colocate", str(publisher)], tmp_path)
    commit_file(publisher, "remote only", "remote-only.txt")
    remote_only_commit = _commit_id(publisher, "@-")
    remote_only_change = _change_id(publisher, "@-")
    recovery_branch = f"review/recovery-{remote_only_change[:8]}"
    run_command(["jj", "git", "remote", "add", "origin", str(remote)], publisher)
    run_command(
        ["jj", "bookmark", "create", recovery_branch, "-r", "@-"],
        publisher,
    )
    run_command(
        ["jj", "git", "push", "--remote", "origin", "--bookmark", recovery_branch],
        publisher,
    )

    assert (
        client.read_remote_git_change_id(
            remote="origin",
            commit_id=remote_only_commit,
        )
        == remote_only_change
    )
    assert client.list_imported_review_bookmarks() == ()

    git_root = Path(run_command(["jj", "git", "root"], repo).stdout.strip())
    temp_ref = "refs/heads/jj-stack-tmp/checkout"
    run_command(
        ["git", "--git-dir", str(git_root), "update-ref", temp_ref, old_commit],
        repo,
    )
    run_command(["jj", "git", "import"], repo)
    assert client.review_temp_ref_target() == old_commit

    with client.import_remote_review_ref(
        remote="origin",
        branch=branch,
        expected_target=old_commit,
        expected_change_id=old_change_id,
    ) as imported:
        assert imported.commit_id == old_commit
        assert imported.change_id == old_change_id
        assert client.review_temp_ref_target() == old_commit
    assert client.review_temp_ref_target() is None
    assert (
        run_command(
            ["jj", "bookmark", "list", "jj-stack-tmp/checkout", "-T", "name"],
            repo,
        ).stdout
        == ""
    )

    client.mutate_remote_review_refs(
        remote="origin",
        updates=(
            ReviewRefUpdate(
                branch=branch,
                desired_target=new_commit,
                expected_target=old_commit,
            ),
            ReviewRefUpdate(
                branch=created_branch,
                desired_target=new_commit,
                expected_target=None,
            ),
        ),
    )
    assert client.list_remote_branches(
        remote="origin",
        patterns=(f"refs/heads/{branch}", f"refs/heads/{created_branch}"),
    ) == {branch: new_commit, created_branch: new_commit}

    client.mutate_remote_review_refs(
        remote="origin",
        updates=(
            ReviewRefUpdate(
                branch=branch,
                desired_target=None,
                expected_target=new_commit,
            ),
            ReviewRefUpdate(
                branch=created_branch,
                desired_target=None,
                expected_target=new_commit,
            ),
        ),
    )
    assert (
        client.list_remote_branches(
            remote="origin",
            patterns=(f"refs/heads/{branch}", f"refs/heads/{created_branch}"),
        )
        == {}
    )
    assert client.list_imported_review_bookmarks() == ()
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


def _change_id(repo: Path, revset: str) -> str:
    return run_command(
        ["jj", "log", "--no-graph", "-r", revset, "-T", "change_id"],
        repo,
    ).stdout.strip()
