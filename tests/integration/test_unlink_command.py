from __future__ import annotations

from pathlib import Path

from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    run_main,
)


def test_unlink_detaches_change_and_preserves_local_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    exit_code = run_main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()
    unlinked_change = state_store.load().changes[change_id]

    assert exit_code == 0
    assert "Stopped review tracking for" in captured.out
    assert unlinked_change.bookmark == bookmark
    assert unlinked_change.link_state == "unlinked"
    assert unlinked_change.pr_number is None
    assert unlinked_change.pr_review_decision is None
    assert unlinked_change.pr_state is None
    assert unlinked_change.pr_url is None
    assert unlinked_change.navigation_comment_id is None
    assert unlinked_change.overview_comment_id is None
    assert JjClient(repo).get_bookmark_state(bookmark).local_target is not None
    assert fake_repo.pull_requests[1].state == "open"
    assert issue_comments(fake_repo, 1) == []


def test_unlink_stays_local_and_does_not_import_drifted_remote_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Unlink must not fetch: a fetch imports remote drift into the local view.

    Promoted from the property scenario
    `move_before:c2:c1,remote_branch_drift:c1,unlinked_change:c2`: unlink used
    to run `jj git fetch`, so a review branch that had moved on the remote was
    imported mid-repair, resurrecting replaced commits and leaving the stack
    divergent with a conflicted bookmark.
    """

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 rewritten"], repo)
    main_target = run_command(
        ["git", "--git-dir", str(fake_repo.git_dir), "rev-parse", "refs/heads/main"],
        fake_repo.git_dir.parent,
    ).stdout.strip()
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{bookmark}",
            main_target,
        ],
        fake_repo.git_dir.parent,
    )

    exit_code = run_main(repo, config_path, "unlink", change_id)
    capsys.readouterr()
    rediscovered = JjClient(repo).discover_review_stack(change_id)

    assert exit_code == 0
    assert state_store.load().changes[change_id].link_state == "unlinked"
    assert not rediscovered.head.divergent
    remembered_remote = JjClient(repo).get_bookmark_state(bookmark).remote_target("origin")
    assert remembered_remote is not None
    assert remembered_remote.target != main_target


def test_unlink_is_idempotent_for_unlinked_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()
    exit_code = run_main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "already unlinked from review tracking" in captured.out


def test_unlink_rejects_change_without_active_review_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    exit_code = run_main(repo, config_path, "unlink", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no active review tracking link to unlink" in captured.err
