from __future__ import annotations

from pathlib import Path

from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.state.store import ReviewStateStore

from ..support.integration_helpers import (
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


def test_cleanup_retires_closed_review_after_local_change_is_abandoned(
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
    assert "remove tracking for" in captured.out
    assert change_id not in ReviewStateStore.for_repo(repo).load().review_identities
    assert not any(
        ref.startswith("refs/heads/jj-stack/") for ref in remote_refs(fake_repo.git_dir)
    )


def test_cleanup_blocks_closed_review_still_claimed_by_native_stack(
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
    fake_repo.native_stacks = {7: (1, 2)}
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)
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

    fake_repo.native_stacks = {}
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
    assert fake_repo.native_stacks == {}


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

    assert exit_code == 1
    assert "open PR #2 still" in captured.out
    assert "rerun cleanup" in " ".join(captured.out.split())
    assert state_store.load() == state
    assert issue_comments(fake_repo, identity.pr_number) == comments_before
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_rechecks_open_dependents_before_deleting_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    identity = state.review_identities[change_id]
    fake_repo.pull_requests[identity.pr_number].state = "closed"
    run_command(["jj", "abandon", change_id], repo)
    calls = 0

    async def dependents_appear_before_delete(self, *, base_refs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return dict.fromkeys(base_refs, ())
        dependent = GithubPullRequest(
            base=GithubBranchRef(ref=identity.head_ref),
            head=GithubBranchRef(ref="manual/late-dependent"),
            html_url="https://github.test/octo-org/stacked-review/pull/2",
            number=2,
            state="open",
            title="late dependent",
        )
        return {ref: (dependent,) for ref in base_refs}

    monkeypatch.setattr(
        GithubClient,
        "get_open_pull_requests_by_base_refs",
        dependents_appear_before_delete,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert calls == 2
    assert "open PR #2 still" in captured.out
    assert state_store.load() == state
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_blocks_duplicate_saved_pr_claims_before_deleting_artifacts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    bottom_change_id, head_change_id = (revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    bottom_identity = state.review_identities[bottom_change_id]
    head_identity = state.review_identities[head_change_id]
    head_baseline = state.submitted_baselines[head_change_id]
    state_store.relink_review(
        head_change_id,
        expected_identity=head_identity,
        expected_baseline=head_baseline,
        identity=head_identity.model_copy(update={"pr_number": bottom_identity.pr_number}),
        baseline=head_baseline,
    )
    ambiguous_state = state_store.load()
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[2].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple tracked changes claim its PR number or branch" in " ".join(
        captured.out.split()
    )
    assert state_store.load() == ambiguous_state
    for identity in ambiguous_state.review_identities.values():
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


def test_cleanup_apply_keeps_remote_branch_when_target_changes_mid_delete(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.review_identities[change_id].head_ref

    fake_repo.pull_requests[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)

    original_mutate = JjClient.mutate_remote_review_refs

    def mutate_with_race(self, *, remote: str, updates) -> None:
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
        original_mutate(self, remote=remote, updates=updates)

    monkeypatch.setattr(
        "jj_stack.jj.client.JjClient.mutate_remote_review_refs",
        mutate_with_race,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert change_id in state_store.load().review_identities
    assert read_remote_ref(fake_repo.git_dir, bookmark) == read_remote_ref(
        fake_repo.git_dir, "main"
    )
    assert "changed before the atomic push" in captured.err


def test_cleanup_removes_managed_stack_comment_for_closed_pull_request(
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

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "delete stack navigation comment" in captured.out
    assert change_id not in refreshed_state.review_identities
    assert issue_comments(fake_repo, 2) == []
