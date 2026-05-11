from __future__ import annotations

from pathlib import Path

from jj_review.jj import JjClient
from jj_review.state.journal import read_operation_log
from jj_review.state.store import ReviewStateStore, resolve_state_path

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
    write_file,
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
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
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
    assert relinked_state.changes[change_id].bookmark == manual_bookmark
    assert relinked_state.changes[change_id].pr_number == 1
    assert relinked_state.changes[change_id].pr_state == "open"
    assert relinked_state.changes[change_id].pr_url == (
        "https://github.test/octo-org/stacked-review/pull/1"
    )

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


def test_relink_accepts_stack_forked_from_trunk_ancestor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    base_commit_id = JjClient(repo).resolve_revision("@-").commit_id

    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)

    run_command(["jj", "new", base_commit_id], repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack(allow_immutable=True)
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    run_command(["jj", "bookmark", "forget", manual_bookmark], repo)
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()
    relinked_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Relinked PR #1" in captured.out
    assert relinked_state.changes[change_id].bookmark == manual_bookmark
    assert relinked_state.changes[change_id].pr_number == 1


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

    assert exit_code == 1
    assert "Could not load pull request #999" in captured.err
    assert "Traceback" not in captured.err


def test_relink_rejects_existing_local_bookmark_on_different_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    write_file(repo / "feature-2.txt", "feature 2\n")
    run_command(["jj", "commit", "-m", "feature 2"], repo)

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    bottom_commit_id = stack.revisions[0].commit_id
    top_change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", bottom_change_id], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )

    exit_code = run_main(repo, config_path, "relink", "1", top_change_id)
    captured = capsys.readouterr()
    bookmark_state = JjClient(repo).get_bookmark_state(manual_bookmark)

    assert exit_code == 1
    assert "already points to a different revision" in captured.err
    assert bookmark_state.local_target == bottom_commit_id


def test_relink_rejects_closed_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    fake_repo.pull_requests[1].state = "closed"

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "is not open" in captured.err


def test_relink_rejects_cross_repository_pull_request_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    fake_repo.pull_requests[1].head_label = f"someone-else:{manual_bookmark}"

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "same-repository pull request branches" in captured.err


def test_relink_rejects_pull_request_with_missing_remote_head_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
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


def test_relink_clears_unlinked_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    captured = capsys.readouterr()
    relinked_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert "Relinked PR #1" in captured.out
    assert relinked_change.link_state == "active"
    assert relinked_change.pr_number == 1
    assert relinked_change.pr_state == "open"


def test_relink_completes_journal_after_successful_relink(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    manual_bookmark = "review/manual-feature-1"
    run_command(["jj", "bookmark", "create", manual_bookmark, "-r", change_id], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", manual_bookmark], repo)
    fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=manual_bookmark,
        title="manual title",
    )
    run_command(["jj", "bookmark", "forget", manual_bookmark], repo)
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 relinked"],
        repo,
    )

    exit_code = run_main(repo, config_path, "relink", "1", change_id)
    capsys.readouterr()

    assert exit_code == 0
    state_dir = resolve_state_path(repo).parent
    assert ReviewStateStore.for_repo(repo).list_operations() == []
    assert tuple((state_dir / "journals").glob("*-relink-*.jsonl")) == ()
    journal_events = tuple(
        event for event in read_operation_log(state_dir) if event.operation == "relink"
    )
    assert journal_events[-1].event == "completed"
