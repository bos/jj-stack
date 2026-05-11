from __future__ import annotations

from pathlib import Path

import pytest

from jj_review.commands import cleanup as cleanup_module
from jj_review.jj import JjClient
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.state.journal import read_operation_log
from jj_review.state.store import ReviewStateStore, resolve_state_path

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
)
from ..support.output_assertions import assert_output_contains, assert_output_in_order
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    read_remote_ref,
    remote_refs,
    run_main,
)


def _mark_pr_state(
    state_store: ReviewStateStore,
    *,
    change_id: str,
    pr_state: str,
) -> None:
    """Set the saved pr_state for a single tracked change."""

    state = state_store.load()
    state_store.save(
        state.model_copy(
            update={
                "changes": {
                    **state.changes,
                    change_id: state.changes[change_id].model_copy(
                        update={"pr_state": pr_state}
                    ),
                }
            }
        )
    )


def test_cleanup_prunes_unlinked_state_for_stale_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "remove tracking for" in captured.out
    assert change_id not in ReviewStateStore.for_repo(repo).load().changes


def test_cleanup_prunes_stale_state_without_a_remote(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path, with_remote=False)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_store.save(ReviewState(changes={change_id: CachedChange()}))

    run_command(["jj", "abandon", change_id], repo)
    monkeypatch.setattr(
        cleanup_module.JjClient,
        "list_git_remotes",
        lambda self: (_ for _ in ()).throw(
            AssertionError("plain cleanup should not resolve remotes for local-only stale state")
        ),
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: unavailable" not in captured.err
    assert "remove tracking for" in captured.out
    assert change_id not in state_store.load().changes


def test_cleanup_forgets_orphan_local_review_bookmark_without_saved_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path, with_remote=False)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    run_command(["jj", "bookmark", "set", "review/orphan-immutable", "-r", "main"], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "forget review/orphan-immutable" in " ".join(captured.out.split())
    assert "review/orphan-immutable" not in run_command(
        ["jj", "bookmark", "list", "review/orphan-immutable"],
        repo,
    ).stdout


def test_cleanup_forgets_orphan_local_bookmark_with_configured_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path, with_remote=False)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=['bookmark_prefix = "bosullivan"'],
    )

    run_command(["jj", "bookmark", "set", "bosullivan/orphan-immutable", "-r", "main"], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "forget bosullivan/orphan-immutable" in " ".join(captured.out.split())
    assert "bosullivan/orphan-immutable" not in run_command(
        ["jj", "bookmark", "list", "bosullivan/orphan-immutable"],
        repo,
    ).stdout


def test_cleanup_keeps_orphan_local_review_bookmark_on_live_reviewable_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path, with_remote=False)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    run_command(["jj", "bookmark", "set", "review/orphan-live", "-r", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No cleanup actions needed." in captured.out
    assert "review/orphan-live" in run_command(
        ["jj", "bookmark", "list", "review/orphan-live"],
        repo,
    ).stdout


def test_cleanup_restack_previews_and_rebases_survivor_above_merged_ancestor(
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

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[1].change_id
    trunk_commit_id = stack.trunk.commit_id
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"

    preview_exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--dry-run",
        "--rebase",
        top_change_id,
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "Planned rebase actions:" in preview.out
    assert f"rebase {top_change_id[:8]} onto trunk()" in preview.out

    apply_exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--rebase",
        top_change_id,
    )
    applied = capsys.readouterr()
    rewritten_top = JjClient(repo).resolve_revision(top_change_id)

    assert apply_exit_code == 0
    assert "Applied rebase actions:" in applied.out
    assert rewritten_top.only_parent_commit_id() == trunk_commit_id
    assert JjClient(repo).resolve_revision(bottom_change_id).commit_id != rewritten_top.commit_id


def test_cleanup_restack_skips_inspection_on_fully_untracked_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    original_fetch_remote = JjClient.fetch_remote
    fetch_calls: list[str] = []

    def tracking_fetch_remote(self, *, remote: str, branches=None) -> None:
        fetch_calls.append(remote)
        return original_fetch_remote(self, remote=remote, branches=branches)

    monkeypatch.setattr(
        "jj_review.review.status.JjClient.fetch_remote",
        tracking_fetch_remote,
    )

    exit_code = run_main(repo, config_path, "cleanup", "--rebase")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No merged changes on the selected stack need rebasing." in captured.out
    assert "Planned rebase actions:" not in captured.out
    assert "Applied rebase actions:" not in captured.out
    assert fetch_calls == []


def test_cleanup_dry_run_reports_stale_tracking_and_remote_branch_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _mark_pr_state(state_store, change_id=change_id, pr_state="closed")
    run_command(["jj", "abandon", change_id], repo)
    run_command(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    captured = capsys.readouterr()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "Planned cleanup actions:" in captured.out
    assert f"remove tracking for {change_id[:8]}" in normalized_output
    assert f"remote branch: delete {bookmark}@origin" in normalized_output
    assert change_id in state_store.load().changes
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_applies_stale_tracking_and_remote_branch_removal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"pr_state": "closed"}
                    ),
                }
            }
        )
    )

    run_command(["jj", "abandon", change_id], repo)
    run_command(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "Applied cleanup actions:" in captured.out
    assert f"remote branch: delete {bookmark}@origin" in normalized_output
    assert change_id not in state_store.load().changes
    assert f"refs/heads/{bookmark}" not in remote_refs(fake_repo.git_dir)


def test_cleanup_preserves_open_orphan_record_and_remote_branch(
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

    run_command(["jj", "abandon", change_id], repo)
    pr_number = state_store.load().changes[change_id].pr_number
    assert pr_number is not None

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "  - preserve open orphan" in captured.out
    assert "preserve open orphan" in normalized_output
    assert f"close --cleanup --pull-request {pr_number}" in normalized_output
    assert change_id in refreshed_state.changes
    assert refreshed_state.changes[change_id].bookmark == bookmark
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_prunes_orphan_record_without_saved_pr_number(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    bookmark = state.changes[change_id].bookmark
    assert bookmark is not None
    state_store.save(
        state.model_copy(
            update={
                "changes": {
                    **state.changes,
                    change_id: state.changes[change_id].model_copy(
                        update={
                            "pr_number": None,
                            "pr_state": None,
                        }
                    ),
                }
            }
        )
    )
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "preserve open orphan" not in captured.out
    assert f"remote branch: delete {bookmark}@origin" not in captured.out
    assert change_id not in state_store.load().changes
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_plans_local_bookmark_forget_before_remote_delete_when_safe(
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
    _mark_pr_state(state_store, change_id=change_id, pr_state="closed")

    run_command(["jj", "bookmark", "set", bookmark, "-r", change_id], repo)
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "  ~ local bookmark: forget " in captured.out
    assert_output_contains(
        captured.out,
        f"{bookmark} (local change is no longer reviewable)",
        f"remote branch: delete {bookmark}@origin",
    )
    assert "  ✗ remote branch:" not in captured.out
    assert_output_in_order(
        captured.out,
        f"local bookmark: forget {bookmark}",
        f"remote branch: delete {bookmark}@origin",
    )
    assert bookmark in run_command(["jj", "bookmark", "list", bookmark], repo).stdout
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_forgets_local_bookmark_before_deleting_remote_branch_when_safe(
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
    _mark_pr_state(state_store, change_id=change_id, pr_state="closed")

    run_command(["jj", "bookmark", "set", bookmark, "-r", change_id], repo)
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert f"local bookmark: forget {bookmark}" in normalized_output
    assert f"remote branch: delete {bookmark}@origin" in normalized_output
    assert change_id not in state_store.load().changes
    assert bookmark not in run_command(["jj", "bookmark", "list", bookmark], repo).stdout
    assert f"refs/heads/{bookmark}" not in remote_refs(fake_repo.git_dir)


def test_cleanup_can_delete_user_bookmarks_when_configured(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=[
            'use_bookmarks = ["potato/custom-feature"]',
            "cleanup_user_bookmarks = true",
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    stack = JjClient(repo).discover_review_stack()
    run_command(
        [
            "jj",
            "bookmark",
            "create",
            "potato/custom-feature",
            "-r",
            stack.revisions[-1].commit_id,
        ],
        repo,
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    state_store = ReviewStateStore.for_repo(repo)
    [tracked_change_id] = list(state_store.load().changes.keys())
    _mark_pr_state(state_store, change_id=tracked_change_id, pr_state="merged")

    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "local bookmark: forget potato/custom-feature" in normalized_output
    assert "remote branch: delete potato/custom-feature@origin" in normalized_output
    assert "potato/custom-feature" not in run_command(
        ["jj", "bookmark", "list", "potato/custom-feature"],
        repo,
    ).stdout
    assert "refs/heads/potato/custom-feature" not in remote_refs(fake_repo.git_dir)


def test_cleanup_apply_batches_remote_delete_local_forget_and_fetch(
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

    stack = JjClient(repo).discover_review_stack()
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    for change_id in change_ids:
        _mark_pr_state(state_store, change_id=change_id, pr_state="closed")
    state = state_store.load()
    bookmarks = tuple(state.changes[change_id].bookmark for change_id in change_ids)
    assert all(bookmark is not None for bookmark in bookmarks)

    for change_id, bookmark in zip(change_ids, bookmarks, strict=True):
        assert bookmark is not None
        run_command(["jj", "bookmark", "set", bookmark, "-r", change_id], repo)

    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    original_delete_remote_bookmarks = JjClient.delete_remote_bookmarks
    original_forget_bookmarks = JjClient.forget_bookmarks
    original_fetch_remote = JjClient.fetch_remote
    calls: list[tuple[str, object]] = []

    def tracking_delete_remote_bookmarks(
        self,
        *,
        remote: str,
        deletions,
        fetch: bool = True,
    ) -> None:
        calls.append(("delete_remote_bookmarks", (remote, tuple(deletions), fetch)))
        return original_delete_remote_bookmarks(
            self,
            remote=remote,
            deletions=deletions,
            fetch=fetch,
        )

    def tracking_forget_bookmarks(self, bookmarks) -> None:
        calls.append(("forget_bookmarks", tuple(bookmarks)))
        return original_forget_bookmarks(self, bookmarks)

    def tracking_fetch_remote(self, *, remote: str, branches=None) -> None:
        calls.append(("fetch_remote", (remote, branches)))
        return original_fetch_remote(self, remote=remote, branches=branches)

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmarks",
        tracking_delete_remote_bookmarks,
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.forget_bookmarks",
        tracking_forget_bookmarks,
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.fetch_remote",
        tracking_fetch_remote,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    capsys.readouterr()

    assert exit_code == 0
    assert calls == [
        (
            "delete_remote_bookmarks",
            (
                "origin",
                tuple(
                    (bookmark, state.changes[change_id].last_submitted_commit_id)
                    for change_id, bookmark in zip(change_ids, bookmarks, strict=True)
                    if bookmark is not None
                ),
                False,
            ),
        ),
        (
            "forget_bookmarks",
            tuple(bookmark for bookmark in bookmarks if bookmark is not None),
        ),
        ("fetch_remote", ("origin", None)),
    ]


def test_cleanup_apply_keeps_remote_branch_when_target_changes_mid_delete(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None

    _mark_pr_state(state_store, change_id=change_id, pr_state="closed")
    run_command(["jj", "abandon", change_id], repo)
    run_command(["jj", "bookmark", "delete", bookmark], repo)

    original_delete_remote_bookmarks = JjClient.delete_remote_bookmarks

    def delete_remote_bookmarks_with_race(
        self,
        *,
        remote: str,
        deletions,
        fetch: bool = True,
    ) -> None:
        bookmark, _expected_remote_target = tuple(deletions)[0]
        run_command(
            [
                "git",
                "--git-dir",
                str(fake_repo.git_dir),
                "update-ref",
                f"refs/heads/{bookmark}",
                read_remote_ref(fake_repo.git_dir, "main"),
            ],
            fake_repo.git_dir.parent,
        )
        original_delete_remote_bookmarks(
            self,
            remote=remote,
            deletions=deletions,
            fetch=fetch,
        )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmarks",
        delete_remote_bookmarks_with_race,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert change_id in state_store.load().changes
    assert read_remote_ref(fake_repo.git_dir, bookmark) == read_remote_ref(
        fake_repo.git_dir, "main"
    )
    assert "force-with-lease" in captured.err


def test_cleanup_apply_preserves_managed_stack_comment_for_closed_pull_request(
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

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[2].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "stack navigation comment" not in captured.out
    assert refreshed_state.changes[change_id].pr_number == 2
    assert refreshed_state.changes[change_id].navigation_comment_id == 2
    assert len(issue_comments(fake_repo, 2)) == 1


def test_cleanup_apply_preserves_discovered_stack_comment_when_cache_id_is_missing(
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

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.pull_requests[2].state = "closed"
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"navigation_comment_id": None}
                    ),
                }
            }
        )
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "stack navigation comment" not in captured.out
    assert refreshed_state.changes[change_id].navigation_comment_id is None
    assert len(issue_comments(fake_repo, 2)) == 1


def test_cleanup_completes_journal_after_successful_apply(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    bookmark = ReviewStateStore.for_repo(repo).load().changes[change_id].bookmark
    assert bookmark is not None

    # Abandon the change and delete the bookmark to make it stale
    run_command(["jj", "abandon", change_id], repo)
    run_command(["jj", "bookmark", "delete", bookmark], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    capsys.readouterr()

    assert exit_code == 0
    state_dir = resolve_state_path(repo).parent
    cleanup_events = [
        event for event in read_operation_log(state_dir) if event.operation == "cleanup"
    ]
    assert [event.event for event in cleanup_events] == ["begin", "completed"]


def test_cleanup_logs_begin_after_failed_apply(
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
    _mark_pr_state(state_store, change_id=change_id, pr_state="closed")

    run_command(["jj", "abandon", change_id], repo)
    run_command(["jj", "bookmark", "delete", bookmark], repo)

    def failing_delete_remote_bookmarks(self, *, remote, deletions, fetch=True):
        raise RuntimeError("Simulated failure during live cleanup")

    monkeypatch.setattr(
        "jj_review.commands.cleanup.JjClient.delete_remote_bookmarks",
        failing_delete_remote_bookmarks,
    )

    with pytest.raises(RuntimeError, match="Simulated failure"):
        run_main(repo, config_path, "cleanup")
    capsys.readouterr()

    state_dir = resolve_state_path(repo).parent
    cleanup_events = [
        event for event in read_operation_log(state_dir) if event.operation == "cleanup"
    ]
    assert [event.event for event in cleanup_events] == ["begin"]
