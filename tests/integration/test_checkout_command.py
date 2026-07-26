from __future__ import annotations

from pathlib import Path

from jj_stack.cli import main
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import FakeGithubRepository
from ..support.integration_helpers import (
    commit_file,
    configure_fake_github_environment,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
)


def test_checkout_bootstraps_tracking_without_importing_review_branches(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    assert _main(repo, config_path, "submit") == 0
    expected = ReviewStateStore.for_repo(repo).load()
    resolve_state_path(repo).unlink()
    capsys.readouterr()

    assert _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2") == 0

    captured = capsys.readouterr()
    assert "Fetched tip commit:" in captured.out
    assert ReviewStateStore.for_repo(repo).load() == expected
    client = JjClient(repo)
    assert client.list_imported_review_bookmarks() == ()
    review_temp = client.review_temp_artifacts()
    assert (review_temp.ref_target, review_temp.bookmark_targets) == (None, ())


def test_checkout_without_fetch_rejects_an_imported_review_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    change_id, identity = next(iter(state.review_identities.items()))
    resolve_state_path(repo).unlink()
    run_command(["jj", "bookmark", "create", identity.head_ref, "-r", change_id], repo)

    assert _main(repo, config_path, "checkout", "--pull-request", "1") == 1

    assert "reserved review/ namespace are imported locally" in capsys.readouterr().err
    assert state_store.load().review_identities == {}
    assert JjClient(repo).list_imported_review_bookmarks() == (identity.head_ref,)


def test_checkout_fetch_rejects_a_locally_rewritten_pull_request_without_importing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    change_id = next(iter(state_store.load().review_identities))
    resolve_state_path(repo).unlink()
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    capsys.readouterr()

    assert _main(repo, config_path, "checkout", "--fetch", "--pull-request", "1") == 1

    captured = capsys.readouterr()
    unwrapped = " ".join(captured.err.split())
    assert "already here at a different commit" in unwrapped
    assert f"jj-stack relink 1 {change_id}" in unwrapped
    assert len(JjClient(repo).query_revisions(f"change_id({change_id})")) == 1


def test_checkout_pull_request_rejects_cross_repository_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    initial_state = ReviewStateStore.for_repo(repo).load()
    fake_repo.pull_requests[2].head_label = f"someone-else:{fake_repo.pull_requests[2].head_ref}"

    assert _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2") == 1

    assert "does not belong to" in capsys.readouterr().err
    assert ReviewStateStore.for_repo(repo).load() == initial_state


def test_checkout_pull_request_rejects_ambiguous_top_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    initial_state = ReviewStateStore.for_repo(repo).load()
    top = fake_repo.pull_requests[2]
    fake_repo.create_pull_request(
        base_ref=top.base_ref,
        body="duplicate",
        head_ref=top.head_ref,
        title="duplicate",
    )

    assert _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2") == 1

    assert "does not uniquely identify" in capsys.readouterr().err
    assert ReviewStateStore.for_repo(repo).load() == initial_state


def test_checkout_rejects_missing_parent_remote_branch_without_partial_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_branch = state.review_identities[stack.revisions[0].change_id].head_ref
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

    assert _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2") == 1

    assert "no longer identify the same commit" in capsys.readouterr().err
    current = ReviewStateStore.for_repo(repo).load()
    assert current.review_identities == {}
    assert current.submitted_baselines == {}


def test_checkout_reports_up_to_date_for_an_already_attached_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)

    assert _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2") == 0

    assert "Local tracking is already up to date for this stack." in capsys.readouterr().out


def test_checkout_pick_uses_saved_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    captured = capsys.readouterr()
    assert "Locally tracked stacks:" in captured.out
    assert "feature 1" in captured.out
    assert "Local tracking is already up to date for this stack." in captured.out


def _configure_checkout_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
) -> Path:
    return configure_fake_github_environment(
        command_modules=(
            "jj_stack.commands.submit.command",
            "jj_stack.review.status",
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
