from __future__ import annotations

from pathlib import Path

from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    read_remote_ref,
    remote_refs,
    run_main,
)


def _mark_unlinked(
    state_store: ReviewStateStore,
    *,
    change_id: str,
) -> None:
    """Unlink a single tracked change, as closing its review through the tool does."""

    review_identity = state_store.load().review_identities[change_id]
    state_store.set_link_state(
        change_id,
        expected_identity=review_identity,
        link_state="unlinked",
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
    assert change_id not in ReviewStateStore.for_repo(repo).load().review_identities


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
    assert (
        "review/orphan-immutable"
        not in run_command(
            ["jj", "bookmark", "list", "review/orphan-immutable"],
            repo,
        ).stdout
    )


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
    assert (
        "review/orphan-live"
        in run_command(
            ["jj", "bookmark", "list", "review/orphan-live"],
            repo,
        ).stdout
    )


def test_cleanup_previews_and_applies_stale_tracking_and_remote_branch_removal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().review_identities[change_id].head_ref

    _mark_unlinked(state_store, change_id=change_id)
    run_command(["jj", "abandon", change_id], repo)
    run_command(["jj", "bookmark", "delete", bookmark], repo)
    fake_repo.native_stacks = {7: (1,)}
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    state_before = state_store.load()

    preview_exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    preview = capsys.readouterr()
    normalized_preview = " ".join(preview.out.split())

    assert preview_exit_code == 1
    assert "Planned cleanup actions:" in preview.out
    assert "preserve PR #1's branch because it remains in GitHub stack #7" in normalized_preview
    assert f"remote branch: delete {bookmark}@origin" not in normalized_preview
    assert state_store.load() == state_before
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)

    blocked_exit_code = run_main(repo, config_path, "cleanup")
    blocked = capsys.readouterr()

    assert blocked_exit_code == 1
    assert "preserve PR #1's branch because it remains in GitHub stack #7" in " ".join(
        blocked.out.split()
    )
    assert change_id in state_store.load().review_identities
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)

    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-23T12:00:00Z"
    apply_exit_code = run_main(repo, config_path, "cleanup")
    applied = capsys.readouterr()
    normalized_applied = " ".join(applied.out.split())

    assert apply_exit_code == 0
    assert "Applied cleanup actions:" in applied.out
    assert f"remote branch: delete {bookmark}@origin" in normalized_applied
    assert change_id not in state_store.load().review_identities
    assert f"refs/heads/{bookmark}" not in remote_refs(fake_repo.git_dir)
    assert fake_repo.native_stacks == {7: (1,)}


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
    bookmark = state_store.load().review_identities[change_id].head_ref

    run_command(["jj", "abandon", change_id], repo)
    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "  - preserve open orphan" in captured.out
    assert "preserve open orphan" in normalized_output
    assert "unstack --cleanup --pull-request orphans" in normalized_output
    assert change_id in refreshed_state.review_identities
    assert refreshed_state.review_identities[change_id].head_ref == bookmark
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_previews_and_applies_local_bookmark_forget_with_remote_delete_when_safe(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().review_identities[change_id].head_ref
    _mark_unlinked(state_store, change_id=change_id)

    run_command(["jj", "bookmark", "set", bookmark, "-r", change_id], repo)
    monkeypatch.setattr(
        "jj_stack.commands.cleanup.command._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )

    preview_exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    preview = capsys.readouterr()
    normalized_preview = " ".join(preview.out.split())

    assert preview_exit_code == 0
    assert (
        f"local bookmark: forget {bookmark} (local change is no longer reviewable)"
        in normalized_preview
    )
    assert f"remote branch: delete {bookmark}@origin" in normalized_preview
    assert "  ✗ remote branch:" not in preview.out
    assert bookmark in run_command(["jj", "bookmark", "list", bookmark], repo).stdout
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)

    apply_exit_code = run_main(repo, config_path, "cleanup")
    applied = capsys.readouterr()
    normalized_applied = " ".join(applied.out.split())

    assert apply_exit_code == 0
    assert f"local bookmark: forget {bookmark}" in normalized_applied
    assert f"remote branch: delete {bookmark}@origin" in normalized_applied
    assert change_id not in state_store.load().review_identities
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
    [tracked_change_id] = list(state_store.load().review_identities)
    _mark_unlinked(state_store, change_id=tracked_change_id)

    monkeypatch.setattr(
        "jj_stack.commands.cleanup.command._stale_change_reasons",
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
    assert (
        "potato/custom-feature"
        not in run_command(
            ["jj", "bookmark", "list", "potato/custom-feature"],
            repo,
        ).stdout
    )
    assert "refs/heads/potato/custom-feature" not in remote_refs(fake_repo.git_dir)


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
    bookmark = initial_state.review_identities[change_id].head_ref

    _mark_unlinked(state_store, change_id=change_id)
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
        "jj_stack.jj.client.JjClient.delete_remote_bookmarks",
        delete_remote_bookmarks_with_race,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert change_id in state_store.load().review_identities
    assert read_remote_ref(fake_repo.git_dir, bookmark) == read_remote_ref(
        fake_repo.git_dir, "main"
    )
    assert "force-with-lease" in captured.err


def test_cleanup_apply_preserves_managed_stack_comment_for_closed_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[2].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "stack navigation comment" not in captured.out
    assert refreshed_state.review_identities[change_id].pr_number == 2
    assert len(issue_comments(fake_repo, 2)) == 1
