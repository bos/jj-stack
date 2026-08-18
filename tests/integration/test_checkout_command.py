from __future__ import annotations

import io
from pathlib import Path

from jj_stack.cli import main
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore, resolve_state_path

from ..support.fake_github import FakeGithubRepo
from ..support.integration_helpers import (
    commit_file,
    configure_fake_github_environment,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
)


def test_checkout_pick_fetches_github_stack_then_adopts_and_edits_selected_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    publisher, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(publisher, "feature 1", "feature-1.txt")
    commit_file(publisher, "feature 2", "feature-2.txt")
    assert _main(publisher, config_path, "submit") == 0
    expected = TrackingStore.for_repo(publisher).load()
    expected_head = selected_stack(publisher).head

    repo = tmp_path / "consumer"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    run_command(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
    client = JjClient(repo)
    client.ensure_pr_branch_fetch_isolation(remote="origin")
    client.fetch_remote(remote="origin")
    run_command(["jj", "bookmark", "create", "main", "-r", "main@origin"], repo)
    assert client.query_commits_by_ids((expected_head.commit_id,)) == ()
    TrackingStore.for_repo(repo).relink_prs(
        replacements={
            change_id: (
                identity,
                expected.submitted_baselines[change_id],
            )
            for change_id, identity in expected.pr_identities.items()
        }
    )
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    captured = capsys.readouterr()
    assert "Available stacks:" in captured.out
    normalized = " ".join(captured.out.split())
    assert "GitHub stack #1 (GitHub only)" in normalized
    assert "Top: PR #2 feature 2" in normalized
    assert "Base: main" in normalized
    assert "Size: 2 PRs" in normalized
    assert "Status: 2 open" in normalized
    assert "Fetched tip commit:" in captured.out
    assert "Working copy now edits" in captured.out
    assert JjClient(repo).resolve_commit("@").change_id == expected_head.change_id
    assert TrackingStore.for_repo(repo).load() == expected
    assert client.visible_pr_bookmark_targets() == {}
    pr_branch_temp = client.pr_branch_temp_artifacts()
    assert (pr_branch_temp.ref_target, pr_branch_temp.bookmark_targets) == (None, ())


def test_checkout_pick_completes_a_partly_local_github_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    publisher, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(publisher, "feature 1", "feature-1.txt")
    commit_file(publisher, "feature 2", "feature-2.txt")
    assert _main(publisher, config_path, "submit") == 0
    expected = TrackingStore.for_repo(publisher).load()

    repo = tmp_path / "consumer"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    run_command(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
    client = JjClient(repo)
    client.ensure_pr_branch_fetch_isolation(remote="origin")
    client.fetch_remote(remote="origin")
    run_command(["jj", "bookmark", "create", "main", "-r", "main@origin"], repo)

    assert _main(repo, config_path, "checkout", "--pull-request", "1") == 0
    assert len(TrackingStore.for_repo(repo).load().pr_identities) == 1
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    output = " ".join(capsys.readouterr().out.split())
    assert output.count("GitHub stack #1") == 1
    assert "GitHub stack #1 (partly local)" in output
    assert TrackingStore.for_repo(repo).load() == expected


def test_checkout_pick_refetches_an_abandoned_tracked_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    run_command(["jj", "abandon", *(change.change_id for change in stack.changes)], repo)
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    output = " ".join(capsys.readouterr().out.split())
    assert "GitHub stack #1 (GitHub only)" in output
    assert JjClient(repo).resolve_commit("@").change_id == stack.head.change_id


def test_checkout_accepts_a_matching_visible_pr_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    state = state_store.load()
    change_id, identity = next(iter(state.pr_identities.items()))
    resolve_state_path(repo).unlink()
    run_command(["jj", "bookmark", "create", identity.head_ref, "-r", change_id], repo)

    assert _main(repo, config_path, "checkout", "--pull-request", "1") == 0

    assert "Updated local tracking for 1 PR" in capsys.readouterr().out
    assert state_store.load().pr_identities == state.pr_identities
    assert set(JjClient(repo).visible_pr_bookmark_targets()) == {identity.head_ref}


def test_checkout_rejects_a_locally_rewritten_pr_before_importing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    change_id = next(iter(state_store.load().pr_identities))
    resolve_state_path(repo).unlink()
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    capsys.readouterr()

    assert _main(repo, config_path, "checkout", "--pull-request", "1") == 1

    captured = capsys.readouterr()
    unwrapped = " ".join(captured.err.split())
    assert "checkout cannot choose between them" in unwrapped
    assert len(JjClient(repo).query_commits(f"change_id({change_id})")) == 1


def test_checkout_rejects_a_rewritten_lower_pr_before_importing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    bottom_change_id = stack.changes[0].change_id
    fake_repo.force_push_pr_head(fake_repo.prs[1])
    resolve_state_path(repo).unlink()
    run_command(["jj", "abandon", stack.head.change_id], repo)
    run_command(
        [
            "git",
            "config",
            "--replace-all",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ],
        repo,
    )
    capsys.readouterr()

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 1

    assert "checkout cannot choose between them" in " ".join(capsys.readouterr().err.split())
    assert len(JjClient(repo).query_commits(f"change_id({bottom_change_id})")) == 1


def test_checkout_pr_rejects_cross_repo_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    initial_state = TrackingStore.for_repo(repo).load()
    fake_repo.prs[2].head_label = f"someone-else:{fake_repo.prs[2].head_ref}"

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 1

    assert "does not belong to" in capsys.readouterr().err
    assert TrackingStore.for_repo(repo).load() == initial_state


def test_checkout_pr_uses_exact_number_when_top_head_is_shared(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    initial_state = TrackingStore.for_repo(repo).load()
    top = fake_repo.prs[2]
    fake_repo.create_pr(
        base_ref="main",
        body="duplicate",
        head_ref=top.head_ref,
        title="duplicate",
    )

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 0

    assert "already up to date" in capsys.readouterr().out
    assert TrackingStore.for_repo(repo).load() == initial_state


def test_checkout_rejects_missing_parent_remote_branch_without_partial_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state = TrackingStore.for_repo(repo).load()
    stack = selected_stack(repo)
    bottom_branch = state.pr_identities[stack.changes[0].change_id].head_ref
    resolve_state_path(repo).unlink()
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{bottom_branch}",
        ],
        repo,
    )

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 1

    assert "no longer identify the same commit" in capsys.readouterr().err
    current = TrackingStore.for_repo(repo).load()
    assert current.pr_identities == {}
    assert current.submitted_baselines == {}


def test_checkout_reports_up_to_date_for_an_already_attached_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 0

    assert "Local tracking is already up to date for this stack." in capsys.readouterr().out


def test_checkout_pick_edits_selected_tracked_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    feature_1_change_id = selected_stack(repo).head.change_id
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    assert _main(repo, config_path, "submit") == 0
    feature_2_change_id = selected_stack(repo).head.change_id
    feature_1_identity = TrackingStore.for_repo(repo).load().pr_identities[feature_1_change_id]
    feature_1_head = fake_repo.ref_target(feature_1_identity.head_ref)
    assert feature_1_head is not None
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "refs/heads/not-managed",
            feature_1_head,
        ],
        repo,
    )
    unmanaged = fake_repo.create_pr(
        base_ref="main",
        head_ref="not-managed",
        title="not adoptable",
        body="",
    )
    fake_repo.github_stacks[9] = (unmanaged.number,)
    capsys.readouterr()

    ordered_heads = sorted((feature_1_change_id, feature_2_change_id))
    selection = ordered_heads.index(feature_1_change_id) + 1
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{selection}\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    captured = capsys.readouterr()
    assert "Available stacks:" in captured.out
    assert "feature 1" in captured.out
    assert "not adoptable" not in captured.out
    assert "Local tracking is already up to date for this stack." in captured.out
    assert "Working copy now edits" in captured.out
    assert JjClient(repo).resolve_commit("@").change_id == feature_1_change_id


def _configure_checkout_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepo,
) -> Path:
    return configure_fake_github_environment(
        command_modules=(
            "jj_stack.commands.submit.command",
            "jj_stack.stack.status",
            "jj_stack.commands.checkout",
        ),
        fake_repo=fake_repo,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


def _main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    return main(
        [
            "--config-file",
            str(config_path),
            "--repository",
            str(repo),
            command,
            *command_args,
        ]
    )
