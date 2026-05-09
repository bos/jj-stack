from __future__ import annotations

import json
from pathlib import Path

import pytest

from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.jj.client import JjCommandError
from jj_review.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
    write_file,
)
from .submit_command_helpers import (
    approve_pull_requests,
    configure_submit_environment,
    patch_github_client_builders,
    read_remote_ref,
    run_main,
)


def _squash_whitespace(text: str) -> str:
    return " ".join(text.split())


def test_land_blocks_unlinked_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "land", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    rendered = _squash_whitespace(captured.out)
    assert "Land blocked:" in rendered
    assert "unlinked from review tracking" in rendered


def test_land_previews_and_finalizes_maximal_ready_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1, 2)

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    bookmark_1 = submitted_state.changes[change_id_1].bookmark
    bookmark_2 = submitted_state.changes[change_id_2].bookmark
    if bookmark_1 is None or bookmark_2 is None:
        raise AssertionError("Expected saved bookmarks after submit.")

    fake_repo.pull_requests[3].state = "closed"

    preview_exit_code = run_main(repo, config_path, "land", "--dry-run")
    preview = capsys.readouterr()
    rendered_preview = _squash_whitespace(preview.out)

    assert preview_exit_code == 0
    assert "push main to feature 2" in rendered_preview
    assert "finalize PR #1" in rendered_preview
    assert "finalize PR #2" in rendered_preview
    assert f"forget {bookmark_1}" in rendered_preview
    assert f"forget {bookmark_2}" in rendered_preview
    assert "before feature 3" in rendered_preview

    apply_exit_code = run_main(repo, config_path, "land")
    applied = capsys.readouterr()
    rendered_applied = _squash_whitespace(applied.out)

    assert apply_exit_code == 0
    assert "Finalizing PR #1 for feature 1" in rendered_applied
    assert "Finalizing PR #2 for feature 2" in rendered_applied
    assert f"forget {bookmark_1}" in rendered_applied
    assert f"forget {bookmark_2}" in rendered_applied
    assert read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[1].commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[3].state == "closed"
    bookmark_states = JjClient(repo).list_bookmark_states((bookmark_1, bookmark_2))
    assert bookmark_states[bookmark_1].local_target is None
    assert bookmark_states[bookmark_2].local_target is None
    assert read_remote_ref(fake_repo.git_dir, bookmark_1) == stack.revisions[0].commit_id
    assert read_remote_ref(fake_repo.git_dir, bookmark_2) == stack.revisions[1].commit_id

    landed_state = state_store.load()
    assert landed_state.changes[change_id_1].pr_state == "merged"
    assert landed_state.changes[change_id_1].navigation_comment_id is None
    assert landed_state.changes[change_id_1].overview_comment_id is None
    assert landed_state.changes[change_id_1].last_submitted_parent_change_id is None
    assert (
        landed_state.changes[change_id_1].last_submitted_stack_head_change_id == change_id_2
    )
    assert landed_state.changes[change_id_2].pr_state == "merged"
    assert landed_state.changes[change_id_2].navigation_comment_id is None
    assert landed_state.changes[change_id_2].overview_comment_id is None
    assert (
        landed_state.changes[change_id_2].last_submitted_parent_change_id == change_id_1
    )
    assert (
        landed_state.changes[change_id_2].last_submitted_stack_head_change_id == change_id_2
    )
    assert landed_state.changes[change_id_3].pr_state == "closed"


def test_land_skip_cleanup_keeps_landed_local_review_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id = stack.revisions[0].change_id
    bookmark = submitted_state.changes[change_id].bookmark
    if bookmark is None:
        raise AssertionError("Expected saved bookmark after submit.")

    exit_code = run_main(repo, config_path, "land", "--skip-cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"forget local bookmark {bookmark}" not in captured.out
    bookmark_state = JjClient(repo).get_bookmark_state(bookmark)
    assert bookmark_state.local_target == stack.revisions[0].commit_id


def test_land_rejects_stack_forked_from_trunk_ancestor(
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

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1)

    original_fetch_remote = JjClient.fetch_remote
    fetch_calls: list[str] = []

    def tracking_fetch_remote(self, *, remote: str, branches=None) -> None:
        fetch_calls.append(remote)
        return original_fetch_remote(self, remote=remote, branches=branches)

    monkeypatch.setattr(
        "jj_review.review.status.JjClient.fetch_remote",
        tracking_fetch_remote,
    )

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error: Selected stack is not based on the current trunk()." in captured.err
    assert "\nHint: No change in the selected stack has landed yet." in captured.err
    assert "jj rebase -s" in captured.err
    assert fetch_calls == ["origin"]


def test_land_reports_current_trunk_drift_after_fetch_instead_of_bookmark_mismatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    other = tmp_path / "other"
    run_command(["git", "clone", str(fake_repo.git_dir), str(other)], tmp_path)
    run_command(["git", "config", "user.name", "Other User"], other)
    run_command(["git", "config", "user.email", "other@example.com"], other)
    write_file(other / "trunk-1.txt", "trunk 1\n")
    run_command(["git", "add", "trunk-1.txt"], other)
    run_command(["git", "commit", "-m", "trunk 1"], other)
    run_command(["git", "push", "origin", "HEAD:main"], other)

    exit_code = run_main(repo, config_path, "land", "--dry-run")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "Error: Selected stack is not based on the current trunk()." in captured.err
    assert "\nHint: No change in the selected stack has landed yet." in captured.err
    assert "jj rebase -s" in captured.err
    assert "cleanup --rebase" not in captured.err
    assert "Local bookmark main points to a different revision" not in combined


def test_land_recommends_cleanup_when_selected_stack_already_has_merged_changes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1, 2)
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-04-18T12:00:00Z"

    other = tmp_path / "other"
    run_command(["git", "clone", str(fake_repo.git_dir), str(other)], tmp_path)
    run_command(["git", "config", "user.name", "Other User"], other)
    run_command(["git", "config", "user.email", "other@example.com"], other)
    write_file(other / "trunk-1.txt", "trunk 1\n")
    run_command(["git", "add", "trunk-1.txt"], other)
    run_command(["git", "commit", "-m", "trunk 1"], other)
    run_command(["git", "push", "origin", "HEAD:main"], other)

    exit_code = run_main(repo, config_path, "land", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error: Selected stack is not based on the current trunk()." in captured.err
    assert "\nHint: Some lower changes from this stack already landed." in captured.err
    assert "cleanup --rebase" in captured.err
    assert "jj rebase -s" not in captured.err


def test_land_defaults_to_at_minus_when_working_copy_is_non_empty(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    write_file(repo / "wip.txt", "wip\n")

    exit_code = run_main(repo, config_path, "land", "--dry-run")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0
    assert "Planned land actions:" in rendered
    assert "finalize PR #1" in rendered
    assert "unlinked from review tracking" not in rendered


def test_land_blocks_unapproved_prefix_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "PR #1 is not approved" in captured.out


def test_land_pull_request_selects_the_landed_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1, 2, 3)

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    bookmark_3 = submitted_state.changes[change_id_3].bookmark
    if bookmark_3 is None:
        raise AssertionError("Expected saved bookmark for feature 3 after submit.")

    exit_code = run_main(repo, config_path, "land", "--pull-request", "2")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0
    assert f"Using PR #2 -> {change_id_2}" in rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[1].commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert fake_repo.pull_requests[3].state == "open"
    assert JjClient(repo).get_bookmark_state(bookmark_3).local_target is not None
    assert list(resolve_state_path(repo).parent.glob("incomplete-*.json")) == []


def test_land_bypass_readiness_previews_and_finalizes_unapproved_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()

    preview_exit_code = run_main(
        repo,
        config_path,
        "land",
        "--bypass-readiness",
        "--dry-run",
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "push main to feature 1" in preview.out

    apply_exit_code = run_main(
        repo,
        config_path,
        "land",
        "--bypass-readiness",
    )
    capsys.readouterr()

    assert apply_exit_code == 0
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[0].commit_id


def test_land_auto_resubmits_rebased_branch_before_landing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id
    old_commit_id = stack.revisions[0].commit_id
    submitted_state = ReviewStateStore.for_repo(repo).load()
    bookmark = submitted_state.changes[change_id].bookmark
    if bookmark is None:
        raise AssertionError("Expected saved bookmark after submit.")

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    rebased_stack = JjClient(repo).discover_review_stack(change_id)
    rebased_commit_id = rebased_stack.revisions[0].commit_id

    assert rebased_commit_id != old_commit_id
    assert read_remote_ref(fake_repo.git_dir, bookmark) == old_commit_id

    preview_exit_code = run_main(repo, config_path, "land", "--dry-run", change_id)
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert f"refresh {bookmark} to match feature 1" in preview.out
    assert "push main to feature 1" in preview.out
    assert read_remote_ref(fake_repo.git_dir, bookmark) == old_commit_id

    apply_exit_code = run_main(repo, config_path, "land", change_id)
    applied = capsys.readouterr()

    assert apply_exit_code == 0
    assert "Refreshing 1 review branch" in applied.out
    assert "Finalizing PR #1 for feature 1" in applied.out
    assert read_remote_ref(fake_repo.git_dir, "main") == rebased_commit_id
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rebased_commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    state = ReviewStateStore.for_repo(repo).load()
    assert state.changes[change_id].pr_state == "merged"


def test_land_blocks_content_divergent_rebased_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id
    old_commit_id = stack.revisions[0].commit_id
    submitted_state = ReviewStateStore.for_repo(repo).load()
    bookmark = submitted_state.changes[change_id].bookmark
    if bookmark is None:
        raise AssertionError("Expected saved bookmark after submit.")

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)
    run_command(["jj", "edit", change_id], repo)
    write_file(repo / "feature-1.txt", "feature 1 with extra tweak\n")
    run_command(["jj", "new"], repo)

    exit_code = run_main(repo, config_path, "land", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    rendered = _squash_whitespace(captured.out)
    assert "Land blocked:" in rendered
    assert "differs from what reviewers approved" in rendered
    assert read_remote_ref(fake_repo.git_dir, bookmark) == old_commit_id


def test_land_blocks_dismissed_approval_after_resubmit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id
    submitted_state = ReviewStateStore.for_repo(repo).load()
    bookmark = submitted_state.changes[change_id].bookmark
    if bookmark is None:
        raise AssertionError("Expected saved bookmark after submit.")

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    original_push = JjClient.push_bookmarks

    def dismissing_push(self, *, remote, bookmarks):
        original_push(self, remote=remote, bookmarks=bookmarks)
        for review in fake_repo.pull_request_reviews[1]:
            review.state = "DISMISSED"

    monkeypatch.setattr(
        "jj_review.jj.JjClient.push_bookmarks",
        dismissing_push,
    )

    rebased_stack = JjClient(repo).discover_review_stack(change_id)
    rebased_commit_id = rebased_stack.revisions[0].commit_id
    trunk_target_before_land = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "land", change_id)
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 1
    assert "Refreshing 1 review branch" in captured.out
    assert "dismissed the approval" in rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_target_before_land
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rebased_commit_id
    assert fake_repo.pull_requests[1].state == "open"


def test_land_blocks_unresolved_conflicted_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "shared.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id

    run_command(["jj", "new", "main"], repo)
    write_file(repo / "shared.txt", "trunk 1\n")
    run_command(["jj", "commit", "-m", "trunk 1"], repo)
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    rebased_stack = JjClient(repo).discover_review_stack(change_id)
    assert rebased_stack.revisions[0].conflict is True

    exit_code = run_main(repo, config_path, "land", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "still has unresolved conflicts" in _squash_whitespace(captured.out)


def test_rebased_partial_land_keeps_descendant_cleanup_path_clear(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1, 2)

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[2].change_id
    fake_repo.pull_requests[3].state = "closed"

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", bottom_change_id, "-d", "main"], repo)

    assert run_main(repo, config_path, "land", top_change_id) == 0
    capsys.readouterr()

    cleanup_exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--dry-run",
        "--rebase",
        top_change_id,
    )
    cleanup = capsys.readouterr()

    assert cleanup_exit_code == 0
    assert "closed without merge" not in _squash_whitespace(cleanup.out)
    assert "No merged changes on the selected stack need rebasing." in cleanup.out


@pytest.mark.parametrize(
    ("push_error", "expected_exit_code", "expected_error"),
    [
        (JjCommandError("simulated trunk push failure"), 1, "simulated trunk push failure"),
        (KeyboardInterrupt(), 130, "Interrupted."),
    ],
)
def test_land_restores_local_trunk_bookmark_when_push_does_not_complete(
    tmp_path: Path,
    monkeypatch,
    capsys,
    push_error: BaseException,
    expected_exit_code: int,
    expected_error: str,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1, 2)

    client = JjClient(repo)
    trunk_before = client.get_bookmark_state("main").local_target
    remote_before = read_remote_ref(fake_repo.git_dir, "main")

    def fail_push_bookmarks(self, *, remote: str, bookmarks) -> None:
        raise push_error

    monkeypatch.setattr(JjClient, "push_bookmarks", fail_push_bookmarks)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == expected_exit_code
    assert expected_error in captured.err
    assert JjClient(repo).get_bookmark_state("main").local_target == trunk_before
    assert read_remote_ref(fake_repo.git_dir, "main") == remote_before


def test_land_replans_after_interrupted_push_when_landable_prefix_changes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1, 2)
    initial_stack = JjClient(repo).discover_review_stack()
    first_landable_commit_id = initial_stack.revisions[0].commit_id

    push_calls = 0
    original_push_bookmarks = JjClient.push_bookmarks

    def fail_first_push_bookmarks(self, *, remote: str, bookmarks) -> None:
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            raise JjCommandError("simulated trunk push failure")
        original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)

    monkeypatch.setattr(JjClient, "push_bookmarks", fail_first_push_bookmarks)

    first_exit_code = run_main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == 1
    assert "simulated trunk push failure" in first_run.err
    [intent_path] = resolve_state_path(repo).parent.glob("incomplete-*.json")
    intent_data = json.loads(intent_path.read_text(encoding="utf-8"))
    intent_data["pid"] = 99999999
    intent_path.write_text(json.dumps(intent_data, indent=2) + "\n", encoding="utf-8")

    fake_repo.pull_requests[2].state = "closed"

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0
    assert "simulated trunk push failure" in first_run.err
    assert "Resuming interrupted" not in second_run.out
    assert read_remote_ref(fake_repo.git_dir, "main") == first_landable_commit_id


def test_land_resumes_after_trunk_push_interruption(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(2):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1, 2)
    submitted_stack = JjClient(repo).discover_review_stack()
    first_change_id = submitted_stack.revisions[0].change_id
    second_change_id = submitted_stack.revisions[1].change_id
    landed_commit_id = submitted_stack.revisions[1].commit_id

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingFinalizeClient(GithubClient):
        get_pull_request_calls = 0

        async def get_pull_request(self, owner: str, repo: str, *, pull_number: int):
            type(self).get_pull_request_calls += 1
            if type(self).get_pull_request_calls == 1:
                raise GithubClientError("simulated PR finalization failure")
            return await super().get_pull_request(owner, repo, pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.land.command",),
        client_type=FailingFinalizeClient,
    )

    first_exit_code = run_main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == 1
    assert "simulated PR finalization failure" in first_run.err
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    [intent_path] = resolve_state_path(repo).parent.glob("incomplete-*.json")
    intent_data = json.loads(intent_path.read_text(encoding="utf-8"))
    intent_data["pid"] = 99999999
    intent_path.write_text(json.dumps(intent_data, indent=2) + "\n", encoding="utf-8")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.land.command",),
    )

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0
    assert f"Resuming interrupted land for {second_change_id[:8]} (from @-)" in second_run.out
    state = ReviewStateStore.for_repo(repo).load()
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert state.changes[first_change_id].pr_state == "merged"
    assert state.changes[second_change_id].pr_state == "merged"
    assert list(resolve_state_path(repo).parent.glob("incomplete-*.json")) == []
