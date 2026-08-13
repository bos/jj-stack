from __future__ import annotations

from pathlib import Path

from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.state.store import ReviewStateStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    read_remote_ref,
    remote_refs,
    run_main,
)


def test_cleanup_removes_closed_review_after_local_change_is_abandoned(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).revisions[-1].change_id
    fake_repo.pull_requests[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1" in captured.out
    assert change_id[:8] in captured.out
    assert change_id not in ReviewStateStore.for_repo(repo).load().review_identities
    assert not any(
        ref.startswith("refs/heads/jj-stack/") for ref in remote_refs(fake_repo.git_dir)
    )


def test_cleanup_revision_only_removes_leftovers_for_selected_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first_change_id = selected_stack(repo).head.change_id

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    second_change_id = selected_stack(repo).head.change_id
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[2].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup", second_change_id)
    state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert second_change_id not in state.review_identities
    assert first_change_id in state.review_identities


def test_cleanup_pull_request_selects_orphaned_saved_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    fake_repo.pull_requests[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup", "--pull-request", "1")

    assert exit_code == 0
    assert change_id not in ReviewStateStore.for_repo(repo).load().review_identities


def test_cleanup_close_finishes_open_and_terminal_orphans(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_ids = [selected_stack(repo).head.change_id]

    for index in (2, 3):
        run_command(["jj", "new", "main"], repo)
        commit_file(repo, f"feature {index}", f"feature-{index}.txt")
        change_ids.append(selected_stack(repo).head.change_id)
        assert run_main(repo, config_path, "submit") == 0
        capsys.readouterr()

    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    identities = tuple(initial_state.review_identities[change_id] for change_id in change_ids)
    fake_repo.pull_requests[identities[1].pr_number].state = "closed"
    merged_pull_request = fake_repo.pull_requests[identities[2].pr_number]
    merged_pull_request.state = "closed"
    merged_pull_request.merged_at = "2026-08-13T12:00:00Z"
    run_command(["jj", "abandon", *change_ids], repo)

    preview_exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--pull-request",
        "orphans",
        "--close",
        "--dry-run",
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert f"close PR #{identities[0].pr_number}" in preview.out
    assert f"close PR #{identities[1].pr_number}" not in preview.out
    assert f"close PR #{identities[2].pr_number}" not in preview.out
    assert all(identity.head_ref in preview.out for identity in identities)
    assert fake_repo.pull_requests[identities[0].pr_number].state == "open"
    assert state_store.load() == initial_state

    exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--pull-request",
        "orphans",
        "--close",
    )
    applied = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert f"close PR #{identities[0].pr_number}" in applied.out
    assert f"close PR #{identities[1].pr_number}" not in applied.out
    assert f"close PR #{identities[2].pr_number}" not in applied.out
    assert all(change_id not in refreshed_state.review_identities for change_id in change_ids)
    assert all(
        f"refs/heads/{identity.head_ref}" not in remote_refs(fake_repo.git_dir)
        for identity in identities
    )
    assert all(
        fake_repo.pull_requests[identity.pr_number].state == "closed" for identity in identities
    )


def test_cleanup_blocks_closed_review_still_claimed_by_github_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    bookmarks = tuple(
        state_store.load().review_identities[change_id].head_ref for change_id in change_ids
    )

    for pull_request in fake_repo.pull_requests.values():
        pull_request.state = "closed"
    run_command(["jj", "abandon", *change_ids], repo)
    fake_repo.github_stacks = {7: (1, 2)}
    state_before = state_store.load()

    preview_exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    preview = capsys.readouterr()
    normalized_preview = " ".join(preview.out.split())

    assert preview_exit_code == 1
    assert "Planned cleanup actions:" in preview.out
    assert "GitHub stack #7 blocks this jj-stack operation" in normalized_preview
    assert all(
        f"remote branch: delete {bookmark}@origin" not in normalized_preview
        for bookmark in bookmarks
    )
    assert state_store.load() == state_before
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )

    blocked_exit_code = run_main(repo, config_path, "cleanup")
    blocked = capsys.readouterr()

    assert blocked_exit_code == 1
    assert "GitHub stack #7 blocks this jj-stack operation" in " ".join(blocked.out.split())
    assert all(change_id in state_store.load().review_identities for change_id in change_ids)
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )

    assert run_main(repo, config_path, "unstack", "--stack", "7") == 0
    capsys.readouterr()
    apply_exit_code = run_main(repo, config_path, "cleanup")
    applied = capsys.readouterr()
    normalized_applied = " ".join(applied.out.split())

    assert apply_exit_code == 0
    assert "Applied cleanup actions:" in applied.out
    assert all(
        f"remote branch: delete {bookmark}@origin" in normalized_applied for bookmark in bookmarks
    )
    assert all(change_id not in state_store.load().review_identities for change_id in change_ids)
    assert all(
        f"refs/heads/{bookmark}" not in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )
    assert fake_repo.github_stacks == {}


def test_cleanup_preserves_closed_review_branch_used_by_open_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    bottom_change_id = selected_stack(repo).revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    identity = state.review_identities[bottom_change_id]
    comments_before = issue_comments(fake_repo, identity.pr_number)
    fake_repo.pull_requests[identity.pr_number].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    output = " ".join(captured.out.split())

    assert exit_code == 1
    assert "open PR #2 still" in output
    assert "rerun cleanup" in output
    assert state_store.load() == state
    assert issue_comments(fake_repo, identity.pr_number) == comments_before
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_isolates_malformed_review_observation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    failed_change_id, cleaned_change_id = (revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.github_stacks = {}
    failed_identity = initial_state.review_identities[failed_change_id]
    cleaned_identity = initial_state.review_identities[cleaned_change_id]
    fake_repo.pull_requests[failed_identity.pr_number].state = "closed"
    fake_repo.pull_requests[cleaned_identity.pr_number].state = "closed"
    original_lookup = GithubClient.get_pull_requests_by_numbers

    async def reject_malformed_review(self, *, pull_numbers):
        numbers = tuple(pull_numbers)
        if failed_identity.pr_number in numbers:
            raise GithubClientError("malformed pull request result")
        return await original_lookup(self, pull_numbers=numbers)

    monkeypatch.setattr(
        GithubClient,
        "get_pull_requests_by_numbers",
        reject_malformed_review,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert f"cannot inspect saved PR #{failed_identity.pr_number}" in " ".join(
        captured.out.split()
    )
    assert failed_change_id in refreshed_state.review_identities
    assert cleaned_change_id not in refreshed_state.review_identities
    assert f"refs/heads/{failed_identity.head_ref}" in remote_refs(fake_repo.git_dir)
    assert f"refs/heads/{cleaned_identity.head_ref}" not in remote_refs(fake_repo.git_dir)


def test_cleanup_stops_later_reviews_after_partial_mutation_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    stack_change_ids = {revision.change_id for revision in stack.revisions}
    ordered_change_ids = tuple(
        change_id
        for change_id in initial_state.review_identities
        if change_id in stack_change_ids
    )
    blocking_change_id, later_change_id = ordered_change_ids
    blocking_identity = initial_state.review_identities[blocking_change_id]
    later_identity = initial_state.review_identities[later_change_id]
    fake_repo.github_stacks = {}
    fake_repo.pull_requests[blocking_identity.pr_number].state = "closed"
    fake_repo.pull_requests[later_identity.pr_number].state = "closed"
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=blocking_identity.pr_number,
    )

    async def reject_comment_delete(**_kwargs) -> bool:
        raise CliError("comment deletion failed")

    monkeypatch.setattr(
        "jj_stack.commands._cleanup_actions.delete_stack_overview_comment",
        reject_comment_delete,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "comment deletion failed" in captured.out
    assert blocking_change_id in refreshed_state.review_identities
    assert later_change_id in refreshed_state.review_identities
    assert f"refs/heads/{blocking_identity.head_ref}" not in remote_refs(fake_repo.git_dir)
    assert f"refs/heads/{later_identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_preserves_open_orphan_record_and_remote_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[0].change_id
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
    assert change_id in refreshed_state.review_identities
    assert refreshed_state.review_identities[change_id].head_ref == bookmark
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_removes_overview_comment_for_closed_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[2].state = "closed"
    fake_repo.github_stacks = {}
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=2,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "delete stack overview comment" in captured.out
    assert change_id not in refreshed_state.review_identities
    assert issue_comments(fake_repo, 2) == []


def test_cleanup_blocks_ambiguous_overview_comments_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    identity = initial_state.review_identities[change_id]
    fake_repo.pull_requests[identity.pr_number].state = "closed"
    for label in ("one", "two"):
        fake_repo.create_issue_comment(
            body=f"{STACK_OVERVIEW_COMMENT_MARKER}\n{label}",
            issue_number=identity.pr_number,
        )
    initial_comments = issue_comments(fake_repo, identity.pr_number)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple candidates" in captured.out
    assert state_store.load() == initial_state
    assert issue_comments(fake_repo, identity.pr_number) == initial_comments
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_blocks_pull_request_head_drift_observed_during_planning(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    identity = initial_state.review_identities[change_id]
    pull_request = fake_repo.pull_requests[identity.pr_number]
    pull_request.state = "closed"
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{identity.head_ref}",
            read_remote_ref(fake_repo.git_dir, "main"),
        ],
        fake_repo.git_dir.parent,
    )
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=identity.pr_number,
    )
    initial_comments = issue_comments(fake_repo, identity.pr_number)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "head no longer matches the saved submitted commit" in " ".join(captured.out.split())
    assert state_store.load() == initial_state
    assert issue_comments(fake_repo, identity.pr_number) == initial_comments
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)
