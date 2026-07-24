from __future__ import annotations

from pathlib import Path

from jj_stack.errors import EXIT_GITHUB
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_manual_pr,
    init_fake_github_repo_with_submitted_feature,
    run_command,
)
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    run_main,
)


def test_relink_repairs_existing_pull_request_link_for_rewritten_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_manual_pr(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    manual_bookmark = fake_repo.pull_requests[1].head_ref
    run_command(["jj", "bookmark", "forget", manual_bookmark], repo)
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )

    exit_code = run_main(
        repo,
        config_path,
        "relink",
        "https://github.test/octo-org/stacked-review/pull/1",
        change_id,
    )
    captured = capsys.readouterr()
    relinked_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Relinked PR #1" in captured.out
    assert relinked_state.review_identities[change_id].head_ref == manual_bookmark
    assert relinked_state.review_identities[change_id].pr_number == 1

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.pull_requests) == {1}
    assert fake_repo.pull_requests[1].title == "feature 1 relinked"
    assert (
        read_remote_ref(fake_repo.git_dir, manual_bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )


def test_relink_replaces_stale_submitted_commit_with_remote_pr_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    identity = initial_state.review_identities[change_id]
    bookmark = identity.head_ref
    remote_pr_head = read_remote_ref(fake_repo.git_dir, bookmark)
    stale_submitted_commit = read_remote_ref(fake_repo.git_dir, "main")
    assert stale_submitted_commit != remote_pr_head
    baseline = initial_state.submitted_baselines[change_id]
    state_store.advance_baseline(
        change_id,
        expected_identity=identity,
        expected_baseline=baseline,
        baseline=baseline.model_copy(update={"commit_id": stale_submitted_commit}),
    )
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature repaired"],
        repo,
    )

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    capsys.readouterr()
    relinked_baseline = state_store.load().submitted_baselines[change_id]

    assert exit_code == 0
    assert relinked_baseline.commit_id == remote_pr_head

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.pull_requests) == {1}


def test_relink_reports_missing_pull_request_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    exit_code = run_main(repo, config_path, "relink", "999", change_id)
    captured = capsys.readouterr()

    assert exit_code == EXIT_GITHUB
    assert "Could not load pull request #999" in captured.err
    assert "Traceback" not in captured.err


def test_relink_rejects_pull_request_branch_for_a_different_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_manual_pr(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    manual_bookmark = fake_repo.pull_requests[1].head_ref

    # The template leaves the PR branch on `feature 1`; stack a new `feature 2`
    # on top so the relink target is a different revision.
    commit_file(repo, "feature 2", "feature-2.txt")
    stack = JjClient(repo).discover_review_stack()
    bottom_commit_id = stack.revisions[0].commit_id
    top_change_id = stack.revisions[-1].change_id

    exit_code = run_main(repo, config_path, "relink", "1", top_change_id)
    captured = capsys.readouterr()
    bookmark_state = JjClient(repo).get_bookmark_state(manual_bookmark)

    assert exit_code == 1
    assert "does not match change" in captured.err
    assert bookmark_state.local_target == bottom_commit_id


def test_relink_rejects_pull_request_with_missing_remote_head_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_manual_pr(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    manual_bookmark = fake_repo.pull_requests[1].head_ref
    run_command(["jj", "bookmark", "forget", manual_bookmark], repo)
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{manual_bookmark}",
        ],
        fake_repo.git_dir.parent,
    )

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "does not exist" in captured.err
