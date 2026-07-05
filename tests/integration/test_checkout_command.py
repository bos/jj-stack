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
    run_command,
)


def test_checkout_bootstraps_local_review_state_from_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    review_bookmarks = sorted(
        {
            change.bookmark
            for change in state_before.changes.values()
            if change.bookmark is not None and change.bookmark.startswith("review/")
        }
    )
    for bookmark in review_bookmarks:
        run_command(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()
    capsys.readouterr()

    exit_code = _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Fetched tip commit:" in captured.out
    state_after = ReviewStateStore.for_repo(repo).load()
    bookmarks_after = sorted(
        {
            change.bookmark
            for change in state_after.changes.values()
            if change.bookmark is not None
        }
    )
    assert bookmarks_after == review_bookmarks
    bookmark_states = JjClient(repo).list_bookmark_states(review_bookmarks)
    assert all(
        bookmark_states[bookmark].local_target is not None for bookmark in review_bookmarks
    )


def test_checkout_current_rejects_remote_branches_without_pull_requests(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    review_bookmarks = sorted(
        {
            change.bookmark
            for change in state_before.changes.values()
            if change.bookmark is not None and change.bookmark.startswith("review/")
        }
    )
    fake_repo.pull_requests.clear()
    for bookmark in review_bookmarks:
        run_command(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()

    exit_code = _main(repo, config_path, "checkout", "--fetch")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "selected head already has a pull request" in captured.err
    assert "Missing pull request for:" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}


def test_checkout_pull_request_rejects_cross_repository_heads(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    fake_repo.pull_requests[2].head_label = f"someone-else:{fake_repo.pull_requests[2].head_ref}"

    exit_code = _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "does not belong to" in captured.err
    assert "same-repository pull request branches" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == state_before.changes


def test_checkout_reports_up_to_date_when_selected_stack_is_already_imported(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    capsys.readouterr()

    exit_code = _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Local tracking is already up to date for this stack." in captured.out
    assert "no changes to review" not in captured.out


def test_checkout_current_fails_closed_when_head_has_no_discoverable_remote_review_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    exit_code = _main(repo, config_path, "checkout")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "current stack has no matching remote pull request" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert not {
        bookmark
        for bookmark in JjClient(repo).list_bookmark_states()
        if bookmark.startswith("review/")
    }


def test_checkout_revset_fails_closed_without_remote_bookmark_identity(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path, with_remote=False)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    exit_code = _main(repo, config_path, "checkout", "--revset", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "selected head already has a pull request" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert not {
        bookmark
        for bookmark in JjClient(repo).list_bookmark_states()
        if bookmark.startswith("review/")
    }


def test_checkout_pull_request_fails_closed_when_head_branch_matches_multiple_pull_requests(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[-1].change_id
    initial_state = ReviewStateStore.for_repo(repo).load()
    top_bookmark = initial_state.changes[top_change_id].bookmark
    assert top_bookmark is not None
    fake_repo.create_pull_request(
        base_ref=fake_repo.pull_requests[2].base_ref,
        body="duplicate link",
        head_ref=top_bookmark,
        title="duplicate link",
    )

    exit_code = _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple pull requests" in captured.err
    assert "view --fetch" in captured.err
    assert "status --fetch" not in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == initial_state.changes


def test_checkout_fails_closed_when_stack_would_need_generated_bookmarks(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    for bookmark in (bottom_bookmark, top_bookmark):
        run_command(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{bottom_bookmark}",
        ],
        repo,
    )

    exit_code = _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "saved branch" in captured.err
    assert "is not present on the selected remote" in captured.err
    assert "view --fetch" in captured.err
    assert "status --fetch" not in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, top_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is None
    assert bookmark_states[top_bookmark].local_target is None


def test_checkout_fails_closed_without_partial_local_bookmark_updates(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    for bookmark in (bottom_bookmark, top_bookmark):
        run_command(["jj", "bookmark", "forget", bookmark], repo)
    main_target = JjClient(repo).resolve_revision("main").commit_id
    run_command(["jj", "bookmark", "set", top_bookmark, "--revision", "main"], repo)

    exit_code = _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already points to a different revision" in captured.err
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, top_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is None
    assert bookmark_states[top_bookmark].local_target == main_target


def test_checkout_prefers_exact_remote_bookmarks_over_stale_cached_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    stale_bookmark = f"review/stale-name-{bottom_change_id[:8]}"
    state_store.save(
        state_before.model_copy(
            update={
                "changes": {
                    **state_before.changes,
                    bottom_change_id: state_before.changes[bottom_change_id].model_copy(
                        update={"bookmark": stale_bookmark}
                    ),
                }
            }
        )
    )
    for bookmark in (bottom_bookmark, top_bookmark):
        run_command(["jj", "bookmark", "forget", bookmark], repo)

    exit_code = _main(repo, config_path, "checkout", "--fetch", "--pull-request", "2")

    assert exit_code == 0
    state_after = state_store.load()
    assert state_after.changes[bottom_change_id].bookmark == bottom_bookmark
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, stale_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is not None
    assert bookmark_states[stale_bookmark].local_target is None


def test_checkout_current_rejects_cache_only_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert _main(repo, config_path, "submit") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = state_before.changes[change_id].bookmark
    assert bookmark is not None

    run_command(["jj", "bookmark", "forget", bookmark], repo)
    fake_repo.pull_requests.clear()
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{bookmark}",
        ],
        repo,
    )

    assert _main(repo, config_path, "checkout", "--fetch") == 1


def test_checkout_revset_rejects_generated_bookmarks_without_selected_remote(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert _main(repo, config_path, "submit") == 0
    state_before = ReviewStateStore.for_repo(repo).load()
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    bottom_bookmark = state_before.changes[bottom_change_id].bookmark
    top_bookmark = state_before.changes[top_change_id].bookmark
    assert bottom_bookmark is not None
    assert top_bookmark is not None

    for bookmark in (bottom_bookmark, top_bookmark):
        run_command(["jj", "bookmark", "forget", bookmark], repo)
    resolve_state_path(repo).unlink()
    run_command(["jj", "git", "remote", "remove", "origin"], repo)

    exit_code = _main(repo, config_path, "checkout", "--revset", top_change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "selected head already has a pull request" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    bookmark_states = JjClient(repo).list_bookmark_states((bottom_bookmark, top_bookmark))
    assert bookmark_states[bottom_bookmark].local_target is None
    assert bookmark_states[top_bookmark].local_target is None


def _configure_checkout_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
    *,
    extra_config_lines: list[str] | None = None,
) -> Path:
    return configure_fake_github_environment(
        command_modules=(
            "jj_stack.commands.submit.command",
            "jj_stack.review.status",
            "jj_stack.commands.checkout",
        ),
        extra_config_lines=extra_config_lines,
        fake_repo=fake_repo,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


def _main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    argv = ["--config-file", str(config_path), "--repository", str(repo), command]
    argv.extend(command_args)
    return main(argv)
