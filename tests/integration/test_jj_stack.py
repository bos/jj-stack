from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.jj.client import JjClient, JjCommandError, ReviewRefUpdate
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.selected import select_review_path

from ..support.integration_helpers import (
    TEST_REVIEW_NAMESPACE,
    commit_file,
    init_repo,
    run_command,
)


def test_selected_path_observes_linear_history_from_default_head(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    path = select_review_path(
        jj_client=JjClient(repo),
        namespace=TEST_REVIEW_NAMESPACE,
        state=ReviewState(),
    )
    stack = path.stack

    assert path.is_maximal
    assert stack.selected_revset == "@-"
    assert [revision.subject for revision in stack.revisions] == ["feature 1", "feature 2"]


@pytest.mark.parametrize("working_copy", ("empty", "undescribed"))
def test_selected_path_maximality_ignores_excluded_working_copy_child(
    tmp_path: Path,
    working_copy: str,
) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature", "feature.txt")
    feature = _current_parent_commit_id(repo)
    if working_copy == "undescribed":
        (repo / "working-copy.txt").write_text("work\n", encoding="utf-8")

    path = select_review_path(
        jj_client=JjClient(repo),
        namespace=TEST_REVIEW_NAMESPACE,
        revset=feature,
        state=ReviewState(),
    )

    assert path.is_maximal


def test_selected_path_ignores_off_path_reviewable_child(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    feature_1 = _current_parent_commit_id(repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    feature_2 = _current_parent_commit_id(repo)
    run_command(["jj", "new", feature_1], repo)
    commit_file(repo, "feature side", "feature-side.txt")

    stack = select_review_path(
        jj_client=JjClient(repo),
        namespace=TEST_REVIEW_NAMESPACE,
        revset=feature_2,
        state=ReviewState(),
    ).stack

    assert [revision.subject for revision in stack.revisions] == ["feature 1", "feature 2"]


def test_paired_ancestor_membership_ignores_an_unavailable_target(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    client = JjClient(repo)
    ancestor = client.resolve_revision("@--").commit_id
    descendant = client.resolve_revision("@-").commit_id

    matching = client.query_paired_ancestor_membership(
        (
            (ancestor, descendant),
            (descendant, ancestor),
            (descendant, "f" * 40),
        )
    )

    assert matching == {ancestor}


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
def test_visible_review_bookmark_does_not_block_broad_operations(
    tmp_path: Path,
    layout_flag: str,
    exposure: str,
) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    run_command(["git", "init", "--bare", str(remote)], tmp_path)
    run_command(["jj", "git", "init", layout_flag, str(repo)], tmp_path)
    commit_file(repo, "base", "base.txt")
    run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    commit_file(repo, "feature", "feature.txt")
    commit_id = _commit_id(repo, "@-")
    change_id = _change_id(repo, "@-")
    branch = f"jj-stack/feature-{change_id[:8]}"
    run_command(["jj", "git", "remote", "add", "origin", str(remote)], repo)
    run_command(["jj", "bookmark", "create", branch, "-r", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", branch], repo)
    run_command(["jj", "bookmark", "forget", branch], repo)
    run_command(["jj", "bookmark", "forget", "--include-remotes", branch], repo)
    run_command(["jj", "git", "export"], repo)
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    client = JjClient(repo)
    client.ensure_review_fetch_isolation(namespace=TEST_REVIEW_NAMESPACE, remote="origin")

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
    assert client.visible_review_bookmark_targets(namespace=TEST_REVIEW_NAMESPACE) == {}

    if exposure == "fetch":
        client.fetch_remote(remote="origin")
    else:
        with client.import_remote_review_ref(
            remote="origin",
            branch=branch,
            namespace=TEST_REVIEW_NAMESPACE,
            expected_target=commit_id,
            expected_change_id=change_id,
        ) as imported:
            assert imported.commit_id == commit_id

    assert set(client.visible_review_bookmark_targets(namespace=TEST_REVIEW_NAMESPACE)) == {
        branch
    }
    assert client.review_temp_artifacts().bookmark_targets == ()
    assert client.review_temp_artifacts().ref_target is None

    state = ReviewState(
        review_identities={
            change_id: ReviewIdentity(
                repository_owner="octo-org",
                repository_name="stacked-review",
                pr_number=1,
                head_owner="octo-org",
                head_ref=branch,
            )
        },
        submitted_baselines={change_id: SubmittedBaseline(commit_id=commit_id)},
    )

    selected = select_review_path(
        jj_client=client,
        namespace=TEST_REVIEW_NAMESPACE,
        revset=change_id,
        state=state,
    ).stack.head

    assert selected.change_id == change_id
    assert selected.commit_id != commit_id
    assert not selected.divergent


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
    branch = f"jj-stack/foundation-{old_change_id[:8]}"
    created_branch = f"jj-stack/created-{new_change_id[:8]}"
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
    visible_review_bookmarks = {branch: frozenset({old_commit})}
    assert (
        client.visible_review_bookmark_targets(namespace=TEST_REVIEW_NAMESPACE)
        == visible_review_bookmarks
    )
    assert client.list_remote_branches(
        remote="origin",
        patterns=(f"refs/heads/{branch}",),
    ) == {branch: old_commit}

    publisher = tmp_path / "publisher"
    run_command(["jj", "git", "init", "--no-colocate", str(publisher)], tmp_path)
    commit_file(publisher, "remote only", "remote-only.txt")
    remote_only_commit = _commit_id(publisher, "@-")
    remote_only_change = _change_id(publisher, "@-")
    recovery_branch = f"jj-stack/recovery-{remote_only_change[:8]}"
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
    assert (
        client.visible_review_bookmark_targets(namespace=TEST_REVIEW_NAMESPACE)
        == visible_review_bookmarks
    )

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
        namespace=TEST_REVIEW_NAMESPACE,
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
        namespace=TEST_REVIEW_NAMESPACE,
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
    stale_targets = {branch: old_commit, created_branch: new_commit}
    with pytest.raises(JjCommandError):
        client.mutate_remote_review_refs(
            namespace=TEST_REVIEW_NAMESPACE,
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
        == stale_targets
    )

    run_command(
        [
            "git",
            "--git-dir",
            str(remote),
            "update-ref",
            f"refs/heads/{branch}",
            new_commit,
        ],
        repo,
    )
    client.mutate_remote_review_refs(
        namespace=TEST_REVIEW_NAMESPACE,
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
    assert (
        client.visible_review_bookmark_targets(namespace=TEST_REVIEW_NAMESPACE)
        == visible_review_bookmarks
    )
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
