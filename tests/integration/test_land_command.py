from __future__ import annotations

import json
from pathlib import Path

import pytest

import jj_stack.commands.land.execute as land_execute
from jj_stack.errors import EXIT_FAILURE, EXIT_INCOMPLETE, CliError
from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.state.journal import read_operation_log
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
    "jj_stack.review.landed",
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
    state_dir = resolve_state_path(repo).parent
    journal_events = tuple(
        event for event in read_operation_log(state_dir) if event.operation == "land"
    )
    assert journal_events[0].event == "begin"
    assert any(
        event.event == "mutation_applied" and event.data["mutation"] == "push_trunk"
        for event in journal_events
    )
    assert journal_events[-1].event == "completed"
    assert set(journal_events[-1].data["retired_change_ids"]) == {change_id_1, change_id_2}

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


def test_land_rerun_after_crash_between_trunk_move_and_push_gives_targeted_hint(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A crash that leaves the local trunk bookmark moved fails closed with a repair hint."""

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

    monkeypatch.setattr(land_execute, "_push_trunk_bookmark", move_trunk_then_crash)

    assert run_main(repo, config_path, "land") == EXIT_FAILURE
    first_run = capsys.readouterr()
    assert "Simulated process death" in first_run.err
    assert JjClient(repo).get_bookmark_state("main").local_target == target_trunk

    monkeypatch.setattr(land_execute, "_push_trunk_bookmark", original_push_trunk)

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == EXIT_FAILURE
    assert "does not match" in second_run.err
    assert "move main back with" in second_run.err

    assert original_trunk is not None
    JjClient(repo).set_bookmark("main", original_trunk, allow_backwards=True)

    third_exit_code = run_main(repo, config_path, "land")
    third_run = capsys.readouterr()

    assert third_exit_code == 0, (third_run.out, third_run.err)
    assert read_remote_ref(fake_repo.git_dir, "main") == target_trunk


def test_land_rerun_after_failed_push_replans_from_current_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A failed trunk push leaves no residue; the rerun replans against live state."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    initial_stack = JjClient(repo).discover_review_stack()
    first_landable_commit_id = initial_stack.revisions[0].commit_id
    trunk_before = JjClient(repo).get_bookmark_state("main").local_target
    remote_before = read_remote_ref(fake_repo.git_dir, "main")

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
    assert JjClient(repo).get_bookmark_state("main").local_target == trunk_before
    assert read_remote_ref(fake_repo.git_dir, "main") == remote_before

    fake_repo.pull_requests[2].state = "closed"

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0, (second_run.out, second_run.err)
    assert "push main to feature 1" in _squash_whitespace(second_run.out)
    assert read_remote_ref(fake_repo.git_dir, "main") == first_landable_commit_id


def test_land_finishes_after_trunk_push_interrupted_before_finalization(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Finalization left behind by an interrupted land converges through sync."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
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
    first_rendered = _squash_whitespace(first_run.out)

    assert first_exit_code == 1
    assert "could not load PR #1" in first_rendered
    assert "current sync can retarget or close PRs for other tracked stacks" in first_rendered
    assert f"jj-stack sync --dry-run {short_change_id(landed_change_ids[0])}" in first_rendered
    assert "Finalizing PR #2 for feature 2" in first_rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    interrupted_state = state_store.load()
    assert landed_change_ids[0] in interrupted_state.changes
    assert landed_change_ids[1] not in interrupted_state.changes

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )

    # Rerunning land converges the leftover even though its own plan is
    # blocked at the unapproved PR #3 — the help-text promise.
    rerun_exit_code = run_main(repo, config_path, "land")
    rerun = capsys.readouterr()
    rerun_rendered = _squash_whitespace(rerun.out)

    assert "Finalizing PR #1" in rerun_rendered
    assert "remove tracking for landed" in rerun_rendered
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    interrupted_state = state_store.load()
    assert landed_change_ids[0] not in interrupted_state.changes
    bookmark_states = JjClient(repo).list_bookmark_states(saved_bookmarks)
    for bookmark in saved_bookmarks:
        assert bookmark_states[bookmark].local_target is None
    # Its own plan stayed blocked (PR #3 unapproved), so the exit code says so.
    assert rerun_exit_code == 1

    # sync then refreshes the surviving stack normally.
    sync_exit_code = run_main(repo, config_path, "sync")
    sync_run = capsys.readouterr()
    assert sync_exit_code == 0, (sync_run.out, sync_run.err)
    assert fake_repo.pull_requests[3].base_ref == "main"


def test_sync_skips_merged_review_whose_pull_request_head_moved(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Even a merged PR is never torn down without proof its head is this review."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    commit_1 = stack.revisions[0].commit_id
    commit_2 = stack.revisions[1].commit_id
    change_id_1 = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark_1 = state_store.load().changes[change_id_1].bookmark
    assert bookmark_1 is not None
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-20T12:00:00Z"
    update_remote_ref(fake_repo, branch="main", target=commit_1)
    update_remote_ref(fake_repo, branch=bookmark_1, target=commit_2)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "head no longer matches" in rendered
    assert change_id_1 in state_store.load().changes


def test_sync_skips_landed_review_whose_pull_request_head_moved(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """The sweep never finalizes a PR whose head no longer identifies the review."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    commit_1 = stack.revisions[0].commit_id
    commit_2 = stack.revisions[1].commit_id
    change_id_1 = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark_1 = state_store.load().changes[change_id_1].bookmark
    assert bookmark_1 is not None
    # The exact commit reached trunk outside the tool, but the review branch
    # (and so the PR head) was force-moved to unrelated work afterwards.
    update_remote_ref(fake_repo, branch="main", target=commit_1)
    update_remote_ref(fake_repo, branch=bookmark_1, target=commit_2)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "skip landed" in rendered
    assert "head no longer matches" in rendered
    assert fake_repo.pull_requests[1].state == "open"
    assert change_id_1 in state_store.load().changes


def test_sweep_skips_landed_review_with_local_edits_since_submit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A landed review whose change was edited locally is reported, never retired.

    The dangerous variant: another workspace's direct push put the exact
    submitted commit on trunk while its PR is still open, and the user edited
    the change here before fetching. Convergence must not close that PR or
    retire its tracking out from under the edits.
    """

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    commit_1 = stack.revisions[0].commit_id
    state_store = ReviewStateStore.for_repo(repo)
    # The exact submitted commit reaches trunk remotely (as an interrupted
    # direct push from elsewhere would leave it); PR #1 stays open.
    update_remote_ref(fake_repo, branch="main", target=commit_1)
    # The user edits the change locally before fetching.
    run_command(["jj", "describe", "-r", change_id_1, "-m", "feature 1 edited"], repo)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out + captured.err)

    # The rebase pass blocks on the local edits, and the sweep independently
    # skips the same review instead of closing its PR or retiring it.
    assert exit_code == 1, (captured.out, captured.err)
    assert "skip landed" in rendered
    assert "does not resolve to one local revision" in rendered
    # PR state is not a usable signal here: the fake auto-marks reachable
    # heads merged (see its idealization note). The contract is that the
    # sweep neither finalized nor retired the edited review.
    assert change_id_1 in state_store.load().changes


def test_land_with_clean_plan_is_not_blocked_by_an_unrelated_straggler(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A skipped straggler from an earlier interruption is advisory, not blocking."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark_1 = state_store.load().changes[change_id_1].bookmark
    assert bookmark_1 is not None

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
    assert run_main(repo, config_path, "land") == 1
    capsys.readouterr()
    assert change_id_1 in state_store.load().changes

    # The straggler's PR is closed without merging (its commit is on trunk),
    # a state the sweep reports and skips on every later run.
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )
    fake_repo.pull_requests[1].state = "closed"
    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 2)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "closed without merge" in rendered
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    # The straggler is untouched and still tracked for inspection.
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is None
    assert change_id_1 in state_store.load().changes


def test_sweep_tolerates_comment_deletion_failure_and_still_retires(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Comment teardown failures leave residue; they never abort convergence."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    commit_1 = stack.revisions[0].commit_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-20T12:00:00Z"
    update_remote_ref(fake_repo, branch="main", target=commit_1)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnCommentDeleteClient(GithubClient):
        async def delete_issue_comment(self, *, comment_id):
            raise GithubClientError("Simulated comment outage", status_code=500)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnCommentDeleteClient,
    )

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "remove tracking for landed" in _squash_whitespace(captured.out)
    assert change_id_1 not in state_store.load().changes


def test_sync_retires_review_merged_outside_the_tool_with_preserved_commit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A merge that preserved the exact commit retires lazily on the next sync."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    commit_1 = stack.revisions[0].commit_id
    change_id_1 = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark_1 = state_store.load().changes[change_id_1].bookmark
    assert bookmark_1 is not None
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"
    update_remote_ref(fake_repo, branch="main", target=commit_1)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "remove tracking for landed" in rendered
    assert change_id_1 not in state_store.load().changes
    assert JjClient(repo).list_bookmark_states((bookmark_1,))[bookmark_1].local_target is None


def test_interrupted_merge_land_is_explained_once_by_the_next_command(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """An unconfirmed merge request is explained by the next run, then forgotten."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailOnMergeClient(GithubClient):
        async def merge_pull_request(self, *, pull_number, merge_method):
            raise GithubClientError("Simulated merge outage", status_code=500)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnMergeClient,
    )

    first_exit_code = run_main(repo, config_path, "land", "--via", "merge")
    first_run = capsys.readouterr()
    assert first_exit_code != 0
    assert "Could not merge PR #1" in first_run.err

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )

    second_exit_code = run_main(repo, config_path, "sync")
    second_run = capsys.readouterr()
    assert second_exit_code == 0, (second_run.out, second_run.err)
    assert "An earlier" in second_run.out
    assert "interrupted before confirming" in second_run.out

    third_exit_code = run_main(repo, config_path, "sync")
    third_run = capsys.readouterr()
    assert third_exit_code == 0
    assert "interrupted before confirming" not in third_run.out


def test_land_via_merge_merges_ready_prefix_bottom_up_on_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Merge-transport landing converges the local stack before returning."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    original_main = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "merge PR #1" in rendered
    assert "merge PR #2" in rendered
    assert "Nothing to submit: everything on the selected stack has merged." in rendered
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    # The second PR was retargeted to trunk before merging.
    assert fake_repo.pull_requests[2].base_ref == "main"
    # GitHub's trunk moved through the merges; jj-stack never pushed it.
    assert read_remote_ref(fake_repo.git_dir, "main") != original_main
    # The merged changes were reconciled out of tracking by the convergence run.
    state = ReviewStateStore.for_repo(repo).load()
    for change_id in change_ids:
        assert change_id not in state.changes


def test_land_via_merge_stops_fail_closed_then_converges_accepted_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    fake_repo.unmergeable_pull_numbers.add(2)
    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    original_commit_2 = stack.revisions[1].commit_id

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not mergeable" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "open"
    assert fake_repo.pull_requests[2].merged_at is None
    # The accepted prefix converged: its tracking retired, and the surviving
    # change was rebased onto the merged trunk and resubmitted.
    state = ReviewStateStore.for_repo(repo).load()
    assert change_id_1 not in state.changes
    surviving_commit = JjClient(repo).resolve_revision(change_id_2).commit_id
    assert surviving_commit != original_commit_2
    assert state.changes[change_id_2].last_submitted_commit_id == surviving_commit
    assert fake_repo.pull_requests[2].base_ref == "main"


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
