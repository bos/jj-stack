from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.jj.client import JjClient, JjCommandError, PRRefUpdate
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR, TrackingState
from jj_stack.stack.selected import select_stack_path

from ..support.integration_helpers import (
    commit_file,
    init_repo,
    jj_commit_id,
    remote_refs,
    run_command,
)


@pytest.mark.parametrize("working_copy", ("empty", "undescribed"))
def test_selected_path_maximality_ignores_excluded_working_copy_child(
    tmp_path: Path,
    working_copy: str,
) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature", "feature.txt")
    feature = jj_commit_id(repo, "@-")
    if working_copy == "undescribed":
        (repo / "working-copy.txt").write_text("work\n", encoding="utf-8")

    path = select_stack_path(
        jj_client=JjClient(repo),
        revset=feature,
        state=TrackingState(),
    )

    assert path.is_maximal


def test_selected_path_ignores_off_path_submittable_child(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    feature_1 = jj_commit_id(repo, "@-")
    commit_file(repo, "feature 2", "feature-2.txt")
    feature_2 = jj_commit_id(repo, "@-")
    run_command(["jj", "new", feature_1], repo)
    commit_file(repo, "feature side", "feature-side.txt")

    stack = select_stack_path(
        jj_client=JjClient(repo),
        revset=feature_2,
        state=TrackingState(),
    ).stack

    assert [change.subject for change in stack.changes] == ["feature 1", "feature 2"]


def test_paired_ancestor_membership_ignores_an_unavailable_target(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    client = JjClient(repo)
    ancestor = client.resolve_commit("@--").commit_id
    descendant = client.resolve_commit("@-").commit_id

    matching = client.query_paired_ancestor_membership(
        (
            (ancestor, descendant),
            (descendant, ancestor),
            (descendant, CommitId("f" * 40)),
        )
    )

    assert matching == {ancestor}


def test_diffstats_batch_preserves_each_commits_files_and_jj_formatting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = init_repo(tmp_path)
    # Windows forbids double quotes in filenames; keep JSON escaping coverage elsewhere.
    filename = "日本語 'quoted'.txt" if os.name == "nt" else '日本語 "quoted".txt'
    commit_file(repo, "first", filename)
    first = jj_commit_id(repo, "@-")
    commit_file(repo, "second", "second.txt")
    second = jj_commit_id(repo, "@-")
    monkeypatch.setenv("COLUMNS", "45")
    run_command(["jj", "config", "set", "--repo", "ui.color", "always"], repo)
    run_command(["jj", "config", "set", "--repo", "ui.log-word-wrap", "true"], repo)
    assert JjClient(repo).resolve_commit(first).subject == "first"
    expected = {
        commit_id: run_command(
            ["jj", "--color", "never", "show", "--stat", "-T", '""', "-r", commit_id],
            repo,
        ).stdout.rstrip()
        for commit_id in (first, second)
    }
    calls = []
    run = subprocess.run

    def counted_run(command, **kwargs):
        calls.append(command)
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", counted_run)
    diffstats = JjClient(repo).diffstats((first, second))

    assert diffstats == expected
    # Batching removes per-change process startup without replacing jj's diff formatter.
    assert len(calls) == 1


def test_list_git_remotes_preserves_distinct_fetch_and_push_urls(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_command(
        [
            "jj",
            "git",
            "remote",
            "add",
            "origin",
            "https://github.test/octo-org/stacked-prs.git",
            "--push-url",
            "git@github.test:octo-org/stacked-prs.git",
        ],
        repo,
    )

    (remote,) = JjClient(repo).list_git_remotes()
    assert remote.name == "origin"
    assert remote.fetch_url == "https://github.test/octo-org/stacked-prs.git"
    assert remote.push_url == "git@github.test:octo-org/stacked-prs.git"


def test_change_id_of_a_non_utf8_git_commit_object_is_still_readable(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run_command(["jj", "git", "remote", "add", "origin", str(tmp_path / "remote.git")], repo)
    git_dir = run_command(["jj", "git", "root"], repo).stdout.strip()
    tree = run_command(
        ["git", "--git-dir", git_dir, "rev-parse", f"{jj_commit_id(repo, '@-')}^{{tree}}"],
        repo,
    ).stdout.strip()
    commit_id = (
        subprocess.run(
            ["git", "--git-dir", git_dir, "hash-object", "-t", "commit", "-w", "--stdin"],
            capture_output=True,
            check=True,
            cwd=repo,
            input=(
                f"tree {tree}\n".encode()
                + b"author Jos\xe9 <j@e.com> 1700000000 +0000\n"
                + b"committer Jos\xe9 <j@e.com> 1700000000 +0000\n"
                + b"change-id qpvuntsmwlqtpsluzzsnyyzlmlwvmwzz\n"
                + b"\ncaf\xe9 subject\n"
            ),
        )
        .stdout.decode()
        .strip()
    )

    commit = JjClient(repo).read_remote_git_commit(remote="origin", commit_id=CommitId(commit_id))

    assert commit.change_id == "qpvuntsmwlqtpsluzzsnyyzlmlwvmwzz"
    assert (commit.author, commit.subject) == ("Jos\ufffd", "caf\ufffd subject")


def test_deleted_tracked_bookmark_does_not_block_stack_observation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    remote = tmp_path / "remote.git"
    run_command(["git", "init", "--bare", str(remote)], tmp_path)
    run_command(["jj", "git", "remote", "add", "origin", str(remote)], repo)
    run_command(["jj", "bookmark", "create", "unrelated", "-r", "main"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "unrelated"], repo)
    run_command(["jj", "bookmark", "delete", "unrelated"], repo)

    path = select_stack_path(jj_client=JjClient(repo), state=TrackingState())

    assert path.stack.changes == ()


def test_visible_pr_bookmark_targets_exclude_removed_conflict_terms(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "left", "left.txt")
    left = jj_commit_id(repo, "@-")
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "right", "right.txt")
    right = jj_commit_id(repo, "@-")
    branch = "jj-stack/conflicted"
    run_command(["jj", "bookmark", "create", branch, "-r", "main"], repo)
    base_operation = run_command(
        ["jj", "op", "log", "--no-graph", "-n", "1", "-T", 'id ++ "\\n"'],
        repo,
    ).stdout.strip()
    run_command(["jj", "bookmark", "set", branch, "-r", left], repo)
    run_command(["jj", f"--at-op={base_operation}", "bookmark", "set", branch, "-r", right], repo)

    assert JjClient(repo).visible_pr_bookmark_targets() == {branch: frozenset({left, right})}


@pytest.mark.parametrize(
    ("layout_flag", "exposure"),
    (("--no-colocate", "import"), ("--colocate", "fetch")),
)
def test_visible_pr_bookmark_does_not_block_broad_operations(
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
    commit_id = jj_commit_id(repo, "@-")
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
    client.ensure_pr_branch_fetch_isolation(remote="origin")

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
    assert client.visible_pr_bookmark_targets() == {}

    if exposure == "fetch":
        client.fetch_remote(remote="origin")
    else:
        with client.import_remote_pr_branch_ref(
            remote="origin",
            branch=branch,
            expected_target=commit_id,
            expected_change_id=change_id,
        ) as imported:
            assert imported.commit_id == commit_id

    assert set(client.visible_pr_bookmark_targets()) == {branch}
    assert client.pr_branch_temp_artifacts().bookmark_targets == ()
    assert client.pr_branch_temp_artifacts().ref_target is None

    state = TrackingState(
        prs={
            ChangeId(change_id): TrackedPR(
                pr_identity=PRIdentity(pr_number=1, head_ref=branch),
                submitted_baseline=SubmittedBaseline(commit_id=commit_id),
            )
        }
    )

    selected = select_stack_path(
        jj_client=client,
        revset=change_id,
        state=state,
    ).stack.head

    assert selected.change_id == change_id
    assert selected.commit_id != commit_id
    assert not selected.divergent
    raw = client.query_commits(f"change_id({change_id})")
    assert client.query_commits_by_change_ids((ChangeId(change_id),))[ChangeId(change_id)] == raw
    assert len(raw) == 2
    assert all(commit.divergent for commit in raw)
    assert next(commit for commit in raw if commit.commit_id == commit_id).immutable


@pytest.mark.parametrize("layout_flag", ("--colocate", "--no-colocate"))
def test_direct_git_pr_branch_ref_operations_use_the_backing_store(
    tmp_path: Path,
    layout_flag: str,
) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    run_command(["git", "init", "--bare", str(remote)], tmp_path)
    run_command(["jj", "git", "init", layout_flag, str(repo)], tmp_path)
    commit_file(repo, "base", "base.txt")
    commit_file(repo, "feature", "feature.txt")
    old_commit = jj_commit_id(repo, "@--")
    new_commit = jj_commit_id(repo, "@-")
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
    visible_pr_bookmarks = {branch: frozenset({old_commit})}
    assert client.visible_pr_bookmark_targets() == visible_pr_bookmarks
    assert remote_refs(remote)[f"refs/heads/{branch}"] == old_commit

    publisher = tmp_path / "publisher"
    run_command(["jj", "git", "init", "--no-colocate", str(publisher)], tmp_path)
    commit_file(publisher, "remote only", "remote-only.txt")
    remote_only_commit = jj_commit_id(publisher, "@-")
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
        client.read_remote_git_commit(
            remote="origin",
            commit_id=remote_only_commit,
        ).change_id
        == remote_only_change
    )
    assert client.visible_pr_bookmark_targets() == visible_pr_bookmarks

    git_root = Path(run_command(["jj", "git", "root"], repo).stdout.strip())
    temp_ref = "refs/heads/jj-stack-tmp/checkout"
    run_command(
        ["git", "--git-dir", str(git_root), "update-ref", temp_ref, old_commit],
        repo,
    )
    run_command(["jj", "git", "import"], repo)
    assert client.pr_branch_temp_ref_target() == old_commit

    with client.import_remote_pr_branch_ref(
        remote="origin",
        branch=branch,
        expected_target=old_commit,
        expected_change_id=old_change_id,
    ) as imported:
        assert imported.commit_id == old_commit
        assert imported.change_id == old_change_id
        assert client.pr_branch_temp_ref_target() == old_commit
    assert client.pr_branch_temp_ref_target() is None
    assert (
        run_command(
            ["jj", "bookmark", "list", "jj-stack-tmp/checkout", "-T", "name"],
            repo,
        ).stdout
        == ""
    )

    client.mutate_remote_pr_branch_refs(
        remote="origin",
        updates=(
            PRRefUpdate(
                branch=branch,
                desired_target=new_commit,
                expected_target=old_commit,
            ),
            PRRefUpdate(
                branch=created_branch,
                desired_target=new_commit,
                expected_target=None,
            ),
        ),
    )
    heads = remote_refs(remote)
    assert heads[f"refs/heads/{branch}"] == new_commit
    assert heads[f"refs/heads/{created_branch}"] == new_commit

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
        client.mutate_remote_pr_branch_refs(
            remote="origin",
            updates=(
                PRRefUpdate(
                    branch=branch,
                    desired_target=None,
                    expected_target=new_commit,
                ),
                PRRefUpdate(
                    branch=created_branch,
                    desired_target=None,
                    expected_target=new_commit,
                ),
            ),
        )
    heads = remote_refs(remote)
    assert {
        branch: heads[f"refs/heads/{branch}"],
        created_branch: heads[f"refs/heads/{created_branch}"],
    } == stale_targets

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
    client.mutate_remote_pr_branch_refs(
        remote="origin",
        updates=(
            PRRefUpdate(
                branch=branch,
                desired_target=None,
                expected_target=new_commit,
            ),
            PRRefUpdate(
                branch=created_branch,
                desired_target=None,
                expected_target=new_commit,
            ),
        ),
    )
    heads = remote_refs(remote)
    assert f"refs/heads/{branch}" not in heads
    assert f"refs/heads/{created_branch}" not in heads
    assert client.visible_pr_bookmark_targets() == visible_pr_bookmarks
    assert (git_root == repo / ".git") is (layout_flag == "--colocate")


def _change_id(repo: Path, revset: str) -> ChangeId:
    return ChangeId(
        run_command(
            ["jj", "log", "--no-graph", "-r", revset, "-T", "change_id"],
            repo,
        ).stdout.strip()
    )
