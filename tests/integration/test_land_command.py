from __future__ import annotations

import json
from pathlib import Path

import pytest

import jj_stack.commands.land.execute as land_execute
from jj_stack.errors import EXIT_FAILURE, EXIT_GITHUB, EXIT_INCOMPLETE, CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.state.journal import OPERATION_LOG_FILENAME, read_operation_log
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    write_file,
)
from ..support.submit_property_harness import update_remote_ref
from .submit_command_helpers import (
    approve_pull_requests,
    configure_submit_environment,
    patch_github_client_builders,
    read_remote_ref,
    run_main,
)

_LAND_CLIENT_MODULES = (
    "jj_stack.commands.land.command",
    "jj_stack.commands.land.recovery",
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
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
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
    assert "remove tracking for landed feature 1" in rendered_applied
    assert "remove tracking for landed feature 2" in rendered_applied
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
    assert change_id_1 not in landed_state.changes
    assert change_id_2 not in landed_state.changes
    assert landed_state.changes[change_id_3].pr_state == "closed"
    state_dir = resolve_state_path(repo).parent
    journal_events = tuple(
        event for event in read_operation_log(state_dir) if event.operation == "land"
    )
    assert journal_events[0].event == "begin"
    assert any(
        event.event == "mutation_applied" and event.data["mutation"] == "push_trunk"
        for event in journal_events
    )
    assert any(
        event.event == "saved_state_update"
        and event.data["change_id"] == change_id_1
        and event.data["after"] is None
        for event in journal_events
    )
    assert any(
        event.event == "saved_state_update"
        and event.data["change_id"] == change_id_2
        and event.data["after"] is None
        for event in journal_events
    )
    assert journal_events[-1].event == "completed"

    list_exit_code = run_main(repo, config_path, "list", "--json")
    listed = capsys.readouterr()
    assert list_exit_code in (0, EXIT_INCOMPLETE)
    listed_change_ids = _list_json_change_ids(listed.out)
    assert change_id_1 not in listed_change_ids
    assert change_id_2 not in listed_change_ids
    assert change_id_3 in listed_change_ids


def _list_json_change_ids(list_output: str) -> set[str]:
    """Every change id `list --json` reports, across stack and orphan rows."""

    rows = json.loads(list_output).get("rows", ())
    change_ids = {
        change["change_id"] for row in rows for change in row.get("changes", ())
    }
    change_ids.update(row["change_id"] for row in rows if row.get("type") == "orphan")
    return change_ids


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
    assert change_id not in state_store.load().changes


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
        "jj_stack.review.status.JjClient.fetch_remote",
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
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
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
    assert change_id not in state.changes


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
        "jj_stack.jj.client.JjClient.push_bookmarks",
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
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
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
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
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


def test_land_retry_repairs_local_trunk_moved_before_interrupted_push(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    client = JjClient(repo)
    original_trunk = client.get_bookmark_state("main").local_target
    target_trunk = client.discover_review_stack().revisions[-1].commit_id
    original_push_trunk = land_execute._push_trunk_bookmark

    def move_trunk_then_crash(*, client, trunk_branch, trunk_revision, **_kwargs):
        client.set_bookmark(trunk_branch, trunk_revision.commit_id)
        raise CliError("Simulated process death before the trunk push")

    monkeypatch.setattr(
        land_execute,
        "_push_trunk_bookmark",
        move_trunk_then_crash,
    )

    assert run_main(repo, config_path, "land") == EXIT_FAILURE
    first_run = capsys.readouterr()
    assert "Simulated process death" in first_run.err
    assert JjClient(repo).get_bookmark_state("main").local_target == target_trunk
    assert ReviewStateStore.for_repo(repo).load().pending_direct_land is not None

    monkeypatch.setattr(
        land_execute,
        "_push_trunk_bookmark",
        original_push_trunk,
    )

    exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert exit_code == 0, (second_run.out, second_run.err)
    assert original_trunk is not None
    assert read_remote_ref(fake_repo.git_dir, "main") == target_trunk
    assert ReviewStateStore.for_repo(repo).load().pending_direct_land is None


def test_land_replans_after_interrupted_push_when_landable_prefix_changes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
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
    pending = ReviewStateStore.for_repo(repo).load().pending_direct_land
    assert pending is not None
    assert pending.phase == "prepared"
    fake_repo.pull_requests[2].state = "closed"

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0
    assert "simulated trunk push failure" in first_run.err
    assert "Resuming interrupted" not in second_run.out
    assert read_remote_ref(fake_repo.git_dir, "main") == first_landable_commit_id
    assert ReviewStateStore.for_repo(repo).load().pending_direct_land is None

    fake_repo.pull_requests[2].state = "open"

    third_exit_code = run_main(repo, config_path, "land")
    third_run = capsys.readouterr()

    assert third_exit_code == 0, (third_run.out, third_run.err)
    assert "push main to feature 2" in _squash_whitespace(third_run.out)

    fourth_exit_code = run_main(repo, config_path, "land")
    fourth_run = capsys.readouterr()

    assert fourth_exit_code == 1
    assert "No changes on the selected stack are ready to land." in fourth_run.out
    assert "saved review identity" not in fourth_run.err


def test_land_replan_replaces_unapplied_pending_transaction(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    original_push_bookmarks = JjClient.push_bookmarks
    push_calls = 0

    def fail_first_push_bookmarks(self, *, remote: str, bookmarks) -> None:
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            raise JjCommandError("simulated trunk push failure")
        original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)

    monkeypatch.setattr(JjClient, "push_bookmarks", fail_first_push_bookmarks)

    assert run_main(repo, config_path, "land") == 1
    first_run = capsys.readouterr()
    assert "simulated trunk push failure" in first_run.err
    first_pending = ReviewStateStore.for_repo(repo).load().pending_direct_land
    assert first_pending is not None

    assert run_main(repo, config_path, "land") == 0
    second_run = capsys.readouterr()
    assert "push main to feature 2" in _squash_whitespace(second_run.out)

    assert ReviewStateStore.for_repo(repo).load().pending_direct_land is None
    completed_first_attempt = tuple(
        event
        for event in read_operation_log(resolve_state_path(repo).parent)
        if event.operation_id == first_pending.operation_id and event.event == "completed"
    )
    assert completed_first_attempt[-1].data["outcome"] == "trunk_not_moved"

    third_exit_code = run_main(repo, config_path, "land")
    third_run = capsys.readouterr()

    assert third_exit_code == 1
    assert "No changes on the selected stack are ready to land." in third_run.out
    assert "saved review identity" not in third_run.err


def test_land_finishes_after_trunk_push_interrupted_before_finalization(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    fake_repo.pull_requests[3].state = "closed"
    stack = JjClient(repo).discover_review_stack()
    landed_commit_id = stack.revisions[1].commit_id
    landed_change_ids = tuple(revision.change_id for revision in stack.revisions[:2])
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    bookmarks = tuple(
        submitted_state.changes[change_id].bookmark for change_id in landed_change_ids
    )
    if any(bookmark is None for bookmark in bookmarks):
        raise AssertionError("Expected saved bookmarks after submit.")
    saved_bookmarks = tuple(bookmark for bookmark in bookmarks if bookmark is not None)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnFinalizeLoadClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            if pull_number == 1:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnFinalizeLoadClient,
    )

    first_exit_code = run_main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == EXIT_GITHUB
    assert "Could not load PR #1 during land" in first_run.err
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert state_store.load().changes[landed_change_ids[0]].pr_state == "open"

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()
    rendered = _squash_whitespace(second_run.out)

    assert second_exit_code == 0
    assert "Finalizing PR #1 for feature 1" in rendered
    assert "Finalizing PR #2 for feature 2" in rendered
    assert "push main to feature 2" not in rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    bookmark_states = JjClient(repo).list_bookmark_states(saved_bookmarks)
    for bookmark in saved_bookmarks:
        assert bookmark_states[bookmark].local_target is None
    finished_state = state_store.load()
    assert landed_change_ids[0] not in finished_state.changes
    assert landed_change_ids[1] not in finished_state.changes


def test_land_preserves_pending_transaction_when_closed_pr_reloads_open(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class ReloadOpenAfterCloseClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            pull_request = await super().get_pull_request(pull_number=pull_number)
            if pull_number != 1:
                return pull_request
            return pull_request.model_copy(
                update={"merged_at": None, "state": "open"}
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=ReloadOpenAfterCloseClient,
    )

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAILURE
    assert "still reports it open after the close request" in captured.err
    pending = state_store.load().pending_direct_land
    assert pending is not None
    assert pending.phase == "trunk_moved"
    assert pending.finalized_change_ids == ()
    for revision in stack.revisions:
        assert revision.change_id in state_store.load().changes


def test_land_recovers_before_inspecting_an_unrelated_selected_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    state_store = ReviewStateStore.for_repo(repo)
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnFinalizeLoadClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            if pull_number == 1:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnFinalizeLoadClient,
    )
    assert run_main(repo, config_path, "land") == EXIT_GITHUB
    capsys.readouterr()

    pending = state_store.load().pending_direct_land
    assert pending is not None
    run_command(["jj", "new", pending.original_trunk_commit_id], repo)
    commit_file(repo, "unrelated left", "unrelated-left.txt")
    left_commit_id = JjClient(repo).resolve_revision("@-").commit_id
    run_command(["jj", "new", pending.original_trunk_commit_id], repo)
    commit_file(repo, "unrelated right", "unrelated-right.txt")
    right_commit_id = JjClient(repo).resolve_revision("@-").commit_id
    run_command(["jj", "new", left_commit_id, right_commit_id], repo)
    commit_file(repo, "unrelated merge", "unrelated-merge.txt")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )
    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "merge commits are not supported" not in captured.out + captured.err
    assert state_store.load().pending_direct_land is None
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[2].state == "closed"


def test_land_retries_after_finalization_before_atomic_state_commit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    original_retire = land_execute._retire_finalized_tracking

    def fail_before_state_commit(**_kwargs) -> None:
        raise CliError("Simulated failure before the direct-land state commit")

    monkeypatch.setattr(
        land_execute,
        "_retire_finalized_tracking",
        fail_before_state_commit,
    )

    first_exit_code = run_main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == EXIT_FAILURE
    assert "Simulated failure before" in first_run.err
    pending = ReviewStateStore.for_repo(repo).load().pending_direct_land
    assert pending is not None
    assert pending.phase == "trunk_moved"
    assert set(pending.finalized_change_ids) == {
        revision.change_id for revision in stack.revisions
    }
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[2].state == "closed"

    monkeypatch.setattr(
        land_execute,
        "_retire_finalized_tracking",
        original_retire,
    )
    fake_repo.pull_request_events.clear()

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0, (second_run.out, second_run.err)
    assert fake_repo.pull_request_events == []
    final_state = ReviewStateStore.for_repo(repo).load()
    assert final_state.pending_direct_land is None
    for revision in stack.revisions:
        assert revision.change_id not in final_state.changes


def test_land_recovery_fails_closed_when_review_branch_moves_after_trunk(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[stack.revisions[0].change_id].bookmark
    if bookmark is None:
        raise AssertionError("Expected a saved review bookmark.")

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnFinalizeLoadClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            if pull_number == 1:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnFinalizeLoadClient,
    )
    assert run_main(repo, config_path, "land") == EXIT_GITHUB
    capsys.readouterr()

    pending = state_store.load().pending_direct_land
    assert pending is not None
    run_command(["jj", "new", pending.original_trunk_commit_id], repo)
    commit_file(repo, "new review work", "new-review-work.txt")
    drift_client = JjClient(repo)
    drift_commit_id = drift_client.resolve_revision("@-").commit_id
    drift_client.set_bookmark("drift-target", drift_commit_id)
    drift_client.push_bookmarks(remote="origin", bookmarks=("drift-target",))
    update_remote_ref(
        fake_repo,
        branch=bookmark,
        target=drift_commit_id,
    )
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )
    fake_repo.pull_request_events.clear()

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAILURE
    assert "review identity" in captured.err
    assert fake_repo.pull_request_events == []
    assert state_store.load().pending_direct_land is not None


def test_land_recovery_requires_finalized_review_branch_to_still_exist(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnSecondFinalizeClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            if pull_number == 2:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnSecondFinalizeClient,
    )
    assert run_main(repo, config_path, "land") == EXIT_GITHUB
    capsys.readouterr()

    pending = state_store.load().pending_direct_land
    assert pending is not None
    first_revision = pending.planned_revisions[0]
    assert first_revision.change_id in pending.finalized_change_ids
    assert fake_repo.pull_requests[1].state == "closed"
    JjClient(repo).delete_remote_bookmarks(
        remote="origin",
        deletions=((first_revision.bookmark, first_revision.commit_id),),
        fetch=False,
    )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )
    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAILURE
    assert "review identity" in captured.err
    assert state_store.load().pending_direct_land is not None
    assert stack.revisions[0].change_id in state_store.load().changes


def test_land_revalidates_pr_head_commit_immediately_before_finalizing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    state_store = ReviewStateStore.for_repo(repo)
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnFinalizeLoadClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            if pull_number == 1:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnFinalizeLoadClient,
    )
    assert run_main(repo, config_path, "land") == EXIT_GITHUB
    capsys.readouterr()

    pending = state_store.load().pending_direct_land
    assert pending is not None
    review_bookmark = pending.planned_revisions[0].bookmark
    run_command(["jj", "new", pending.original_trunk_commit_id], repo)
    commit_file(repo, "racing review head", "racing-review-head.txt")
    drift_commit_id = JjClient(repo).resolve_revision("@-").commit_id
    JjClient(repo).set_bookmark("drift-target", drift_commit_id)
    JjClient(repo).push_bookmarks(remote="origin", bookmarks=("drift-target",))

    class MoveHeadBeforeFinalizeClient(GithubClient):
        first_pull_request_loads = 0

        async def get_pull_request(self, *, pull_number):
            if pull_number == 1:
                self.first_pull_request_loads += 1
                if self.first_pull_request_loads == 2:
                    update_remote_ref(
                        fake_repo,
                        branch=review_bookmark,
                        target=drift_commit_id,
                    )
            return await super().get_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=MoveHeadBeforeFinalizeClient,
    )
    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAILURE
    assert "head no longer matches" in captured.err
    assert state_store.load().pending_direct_land is not None
    assert fake_repo.ref_target(review_bookmark) == drift_commit_id


def test_land_recovers_when_trunk_push_succeeds_but_acknowledgement_is_lost(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    landed_commit_id = stack.revisions[-1].commit_id
    original_local_main = JjClient(repo).get_bookmark_state("main").local_target
    original_push_bookmarks = JjClient.push_bookmarks
    lost_acknowledgement = False

    def push_then_lose_acknowledgement(self, *, remote: str, bookmarks) -> None:
        nonlocal lost_acknowledgement
        original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)
        if bookmarks == ("main",) and not lost_acknowledgement:
            lost_acknowledgement = True
            raise JjCommandError("simulated lost trunk push acknowledgement")

    monkeypatch.setattr(JjClient, "push_bookmarks", push_then_lose_acknowledgement)

    first_exit_code = run_main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == 1
    assert "simulated lost trunk push acknowledgement" in first_run.err
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert JjClient(repo).get_bookmark_state("main").local_target == original_local_main

    monkeypatch.setattr(JjClient, "push_bookmarks", original_push_bookmarks)

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()
    rendered = _squash_whitespace(second_run.out)

    assert second_exit_code == 0, (second_run.out, second_run.err)
    assert "move main to the current trunk() after the interrupted push" in rendered
    assert "push main to feature 2" not in rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert JjClient(repo).get_bookmark_state("main").local_target == landed_commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[2].state == "closed"
    state = ReviewStateStore.for_repo(repo).load()
    assert state.pending_direct_land is None
    for revision in stack.revisions:
        assert revision.change_id not in state.changes
    journal_events = read_operation_log(resolve_state_path(repo).parent)
    assert any(
        event.event == "mutation_applied"
        and event.data.get("mutation") == "repair_local_trunk"
        for event in journal_events
    )
    direct_push_begin = next(
        event
        for event in journal_events
        if event.operation == "land"
        and event.event == "begin"
        and event.data["resolved_scope"]["push_trunk"]
    )
    assert journal_events[-1].event == "completed"
    assert journal_events[-1].operation_id == direct_push_begin.operation_id

    third_exit_code = run_main(repo, config_path, "land")
    third_run = capsys.readouterr()

    assert third_exit_code == 1
    assert "No changes on the selected stack are ready to land." in third_run.out
    assert "saved review identity" not in third_run.err


def test_land_does_not_reactivate_completed_checkpoint_from_audit_log(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    landed_commit_id = JjClient(repo).discover_review_stack().revisions[-1].commit_id
    original_push_bookmarks = JjClient.push_bookmarks
    lost_acknowledgement = False

    def push_then_lose_acknowledgement(self, *, remote: str, bookmarks) -> None:
        nonlocal lost_acknowledgement
        original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)
        if bookmarks == ("main",) and not lost_acknowledgement:
            lost_acknowledgement = True
            raise JjCommandError("simulated lost trunk push acknowledgement")

    monkeypatch.setattr(JjClient, "push_bookmarks", push_then_lose_acknowledgement)
    assert run_main(repo, config_path, "land") == 1
    capsys.readouterr()
    monkeypatch.setattr(JjClient, "push_bookmarks", original_push_bookmarks)

    assert run_main(repo, config_path, "land") == 0
    capsys.readouterr()

    # Audit completion is deliberately outside the state commit point.
    log_path = resolve_state_path(repo).parent / OPERATION_LOG_FILENAME
    lines = log_path.read_text(encoding="utf-8").splitlines()
    dropped_event = json.loads(lines[-1])
    assert dropped_event["event"] == "completed"
    log_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    fake_repo.pull_request_events.clear()

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert "No changes on the selected stack are ready to land." in captured.out
    assert fake_repo.pull_request_events == []
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert ReviewStateStore.for_repo(repo).load().pending_direct_land is None


def test_land_ignores_missing_audit_completion_after_state_commit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    landed_commit_id = stack.revisions[1].commit_id
    landed_change_ids = tuple(revision.change_id for revision in stack.revisions[:2])
    state_store = ReviewStateStore.for_repo(repo)

    assert run_main(repo, config_path, "land") == 0
    capsys.readouterr()

    # Reproduce a crash between retiring the landed tracking and writing the
    # completed marker: drop the trailing completed event from the journal.
    log_path = resolve_state_path(repo).parent / OPERATION_LOG_FILENAME
    lines = log_path.read_text(encoding="utf-8").splitlines()
    dropped_event = json.loads(lines[-1])
    assert (dropped_event["operation"], dropped_event["event"]) == ("land", "completed")
    log_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    fake_repo.pull_request_events.clear()

    exit_code = run_main(repo, config_path, "land")
    capsys.readouterr()

    assert exit_code == 1
    assert fake_repo.pull_request_events == []
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert fake_repo.pull_requests[3].state == "open"
    finished_state = state_store.load()
    for change_id in landed_change_ids:
        assert change_id not in finished_state.changes
    assert state_store.load().pending_direct_land is None


def test_land_resume_fails_closed_when_saved_tracking_pruned_externally(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    landed_commit_id = stack.revisions[1].commit_id
    landed_change_ids = tuple(revision.change_id for revision in stack.revisions[:2])
    state_store = ReviewStateStore.for_repo(repo)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnFinalizeLoadClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            if pull_number == 1:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnFinalizeLoadClient,
    )

    assert run_main(repo, config_path, "land") == EXIT_GITHUB
    capsys.readouterr()
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id

    # Another command removes the saved records without atomically clearing the
    # pending transaction. Recovery must fail closed instead of finalizing PRs
    # whose linkage it can no longer prove.
    state = state_store.load()
    pruned_changes = dict(state.changes)
    for change_id in landed_change_ids:
        del pruned_changes[change_id]
    state_store.save(state.model_copy(update={"changes": pruned_changes}))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )
    fake_repo.pull_request_events.clear()

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == EXIT_FAILURE
    assert "review identity" in captured.err
    assert all(
        event.reason == "head_reachable_from_base"
        for event in fake_repo.pull_request_events
    )
    assert state_store.load().pending_direct_land is not None


def test_land_via_merge_merges_ready_prefix_bottom_up_on_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    original_stack_commits = tuple(revision.commit_id for revision in stack.revisions)
    original_main = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "merge PR #1" in captured.out
    assert "merge PR #2" in captured.out
    assert "run sync" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    # The second PR was retargeted to trunk before merging.
    assert fake_repo.pull_requests[2].base_ref == "main"
    # GitHub's trunk moved through squash merges; jj-stack never pushed it.
    assert read_remote_ref(fake_repo.git_dir, "main") != original_main
    # Local history is untouched; sync/cleanup --rebase is the follow-up.
    refreshed_commits = tuple(
        JjClient(repo).resolve_revision(revision.change_id).commit_id
        for revision in stack.revisions
    )
    assert refreshed_commits == original_stack_commits
    state = ReviewStateStore.for_repo(repo).load()
    for revision in stack.revisions:
        assert state.changes[revision.change_id].pr_state == "merged"


def test_land_via_merge_stops_fail_closed_at_unmergeable_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    fake_repo.unmergeable_pull_numbers.add(2)
    stack = JjClient(repo).discover_review_stack()

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not mergeable" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "open"
    assert fake_repo.pull_requests[2].merged_at is None
    state = ReviewStateStore.for_repo(repo).load()
    assert state.changes[stack.revisions[0].change_id].pr_state == "merged"
    assert state.changes[stack.revisions[1].change_id].pr_state == "open"


def test_land_via_merge_dry_run_previews_merges_without_mutating(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    original_main = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "land", "--dry-run", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "merge PR #1" in captured.out
    assert "Planned land actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert read_remote_ref(fake_repo.git_dir, "main") == original_main


def test_land_classifies_protected_branch_push_rejection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A GH006 rejection surfaces the reason and the matching next step."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    original_trunk_target = JjClient(repo).get_bookmark_state("main").local_target
    hooks_dir = fake_repo.git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        'echo "GH006: Protected branch update failed for refs/heads/main." >&2\n'
        'echo "7 of 7 required status checks are expected." >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GH006: Protected branch update failed" in captured.err
    assert "required status checks are expected" in captured.err
    assert "Wait for the review-branch checks to finish" in captured.err
    assert "land --via merge would not help" in captured.err
    # The failed push restored the local trunk bookmark and the PR is intact.
    assert JjClient(repo).get_bookmark_state("main").local_target == original_trunk_target
    assert fake_repo.pull_requests[1].state == "open"
