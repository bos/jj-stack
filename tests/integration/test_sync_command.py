from __future__ import annotations

import json
from pathlib import Path

import pytest

from jj_stack.errors import EXIT_USAGE
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.models.github import GithubPullRequest
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    write_file,
)
from ..support.submit_property_harness import update_remote_ref
from .submit_command_helpers import (
    approve_pull_requests,
    configure_submit_environment,
    read_remote_ref,
    run_main,
)


def _merge_pull_request(fake_repo, pull_number: int) -> None:
    fake_repo.apply_squash_merge(fake_repo.pull_requests[pull_number])


def test_sync_dry_run_previews_rebase_and_skips_submit_preview(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[1].change_id
    top_commit_id = stack.revisions[1].commit_id
    original_base_ref = fake_repo.pull_requests[2].base_ref
    _merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", "--dry-run", top_change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would remove landed changes from the bottom" in captured.out
    assert "Run sync without --dry-run" in captured.out
    assert "remaining existing PRs" in captured.out
    assert JjClient(repo).resolve_revision(top_change_id).commit_id == top_commit_id
    assert fake_repo.pull_requests[2].base_ref == original_base_ref


def test_sync_reports_nothing_to_submit_when_whole_stack_merged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "Nothing to submit: everything in this stack has landed." in captured.out
    assert JjClient(repo).resolve_revision("@").only_parent_commit_id() == read_remote_ref(
        fake_repo.git_dir, "main"
    )
    # No replacement pull request was opened for the merged change.
    assert set(fake_repo.pull_requests) == {1}


def test_sync_completes_the_protected_trunk_flow_after_land_via_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """land --via merge converges the protected-trunk flow before returning;
    a follow-up sync finds nothing left to repair."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[1].change_id

    land_exit_code = run_main(repo, config_path, "land", "--via", "merge")
    capsys.readouterr()
    assert land_exit_code == 0
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "open"
    # The in-command convergence already rebased the survivor onto the
    # squash-merged trunk tip and retargeted its PR.
    merged_trunk_commit = read_remote_ref(fake_repo.git_dir, "main")
    rewritten_top = JjClient(repo).resolve_revision(top_change_id)
    assert rewritten_top.only_parent_commit_id() == merged_trunk_commit
    assert fake_repo.pull_requests[2].base_ref == "main"

    sync_exit_code = run_main(repo, config_path, "sync", top_change_id)
    captured = capsys.readouterr()

    assert sync_exit_code == 0
    assert "No landed changes in this stack need rebasing." in captured.out
    # Convergence is idempotent: the survivor did not move again.
    assert JjClient(repo).resolve_revision(top_change_id).commit_id == rewritten_top.commit_id
    assert fake_repo.pull_requests[2].state == "open"


@pytest.mark.landing_recovery
def test_sync_rejects_reviewed_sibling_paths_before_recovery_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    initial = JjClient(repo).discover_review_stack()
    landed = initial.revisions[0]
    first_sibling = initial.revisions[1]
    second_sibling = initial.revisions[2]
    run_command(
        ["jj", "rebase", "-s", second_sibling.change_id, "-d", landed.change_id],
        repo,
    )
    jj = JjClient(repo)
    original_first = jj.resolve_revision(first_sibling.change_id).commit_id
    original_second = jj.resolve_revision(second_sibling.change_id).commit_id
    _merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", first_sibling.change_id)
    captured = capsys.readouterr()

    state = ReviewStateStore.for_repo(repo).load()
    assert exit_code == 1
    assert "Reviewed changes belong to more than one local stack" in captured.err
    assert landed.change_id in state.review_identities
    assert jj.resolve_revision(first_sibling.change_id).commit_id == original_first
    assert jj.resolve_revision(second_sibling.change_id).commit_id == original_second


@pytest.mark.landing_recovery
def test_sync_rejects_a_reviewed_unreviewed_reviewed_sandwich_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    landed, reviewed = stack.revisions
    run_command(["jj", "new", landed.change_id], repo)
    commit_file(repo, "local middle", "local-middle.txt")
    local_middle = JjClient(repo).discover_review_stack().head
    run_command(["jj", "rebase", "-r", reviewed.change_id, "-d", local_middle.change_id], repo)
    reviewed_before = JjClient(repo).resolve_revision(reviewed.change_id).commit_id
    middle_before = JjClient(repo).resolve_revision(local_middle.change_id).commit_id
    _merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "appears above an unreviewed change" in captured.err
    assert JjClient(repo).resolve_revision(reviewed.change_id).commit_id == reviewed_before
    assert JjClient(repo).resolve_revision(local_middle.change_id).commit_id == middle_before
    assert set(fake_repo.pull_requests) == {1, 2}


@pytest.mark.landing_recovery
def test_sync_rejects_an_unselected_merge_descendant_before_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    landed, reviewed = JjClient(repo).discover_review_stack().revisions
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "side change", "side.txt")
    side = JjClient(repo).resolve_revision("@-")
    run_command(["jj", "new", reviewed.change_id, side.change_id], repo)
    commit_file(repo, "local merge", "merge.txt")
    local_merge = JjClient(repo).resolve_revision("@-")
    working_copy = JjClient(repo).resolve_revision("@")
    _merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Other local changes depend on this stack" in captured.err
    jj = JjClient(repo)
    assert jj.resolve_revision(local_merge.change_id).commit_id == local_merge.commit_id
    assert jj.resolve_revision("@").commit_id == working_copy.commit_id

    assert run_main(repo, config_path, "sync", landed.change_id) == 0
    captured = capsys.readouterr()
    assert "then rerun sync" in captured.out
    assert "dependent" in captured.out
    assert landed.change_id in ReviewStateStore.for_repo(repo).load().review_identities


@pytest.mark.landing_recovery
def test_sync_rebases_trailing_local_work_without_creating_a_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    initial = JjClient(repo).discover_review_stack()
    landed, reviewed = initial.revisions
    commit_file(repo, "local trailing", "local-trailing.txt")
    trailing = JjClient(repo).discover_review_stack().head
    state_before = ReviewStateStore.for_repo(repo).load()
    assert trailing.change_id not in state_before.review_identities
    _merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", trailing.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    jj = JjClient(repo)
    rewritten_reviewed = jj.resolve_revision(reviewed.change_id)
    rewritten_trailing = jj.resolve_revision(trailing.change_id)
    assert rewritten_reviewed.only_parent_commit_id() == read_remote_ref(
        fake_repo.git_dir, "main"
    )
    assert rewritten_trailing.only_parent_commit_id() == rewritten_reviewed.commit_id
    assert set(fake_repo.pull_requests) == {1, 2}
    assert trailing.change_id not in ReviewStateStore.for_repo(repo).load().review_identities


@pytest.mark.landing_recovery
def test_sync_requires_every_surviving_review_before_rewriting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    reviewed = stack.revisions[1]
    reviewed_before = reviewed.commit_id
    _merge_pull_request(fake_repo, 1)
    del fake_repo.pull_requests[2]

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no longer reports PR #2" in captured.err
    assert JjClient(repo).resolve_revision(reviewed.change_id).commit_id == reviewed_before
    assert set(fake_repo.pull_requests) == {1}


@pytest.mark.landing_recovery
def test_sync_all_isolates_a_head_mismatch_from_an_exact_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    first, second, third = stack.revisions
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    state_path = resolve_state_path(repo)
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["review_identities"]["incomplete-change"] = (
        initial_state.review_identities[first.change_id]
        .model_copy(update={"head_ref": "review/incomplete-change", "pr_number": 99})
        .model_dump(mode="json")
    )
    missing_change_ids = tuple(f"missing-change-{index:02d}" for index in range(64))
    for index, change_id in enumerate(missing_change_ids, start=100):
        raw_state["review_identities"][change_id] = (
            initial_state.review_identities[first.change_id]
            .model_copy(update={"head_ref": f"review/{change_id}", "pr_number": index})
            .model_dump(mode="json")
        )
        raw_state["submitted_baselines"][change_id] = (
            initial_state.submitted_baselines[first.change_id]
            .model_copy(update={"commit_id": f"{index:040x}"})
            .model_dump(mode="json")
        )
    malformed_change_id = "malformed-baseline"
    raw_state["review_identities"][malformed_change_id] = (
        initial_state.review_identities[first.change_id]
        .model_copy(update={"head_ref": "review/malformed-baseline", "pr_number": 164})
        .model_dump(mode="json")
    )
    raw_state["submitted_baselines"][malformed_change_id] = (
        initial_state.submitted_baselines[first.change_id]
        .model_copy(update={"commit_id": "bad'commit"})
        .model_dump(mode="json")
    )
    write_file(state_path, json.dumps(raw_state))

    usage_exit = run_main(repo, config_path, "sync", "--all", second.change_id)
    usage = capsys.readouterr()
    assert usage_exit == EXIT_USAGE
    assert "either" in usage.err

    fake_repo.auto_merge_reachable_heads = False
    fake_repo.pull_requests[3].state = "closed"
    update_remote_ref(fake_repo, branch="main", target=second.commit_id)
    update_remote_ref(
        fake_repo,
        branch=initial_state.review_identities[first.change_id].head_ref,
        target=second.commit_id,
    )

    original_batch = GithubClient.get_pull_requests_by_numbers
    original_single = GithubClient.get_pull_request
    failed_batch = False

    async def fail_first_multi_lookup(self, *, pull_numbers):
        nonlocal failed_batch
        numbers = tuple(pull_numbers)
        if len(numbers) > 1 and not failed_batch:
            failed_batch = True
            raise GithubClientError("forced batch failure")
        return await original_batch(self, pull_numbers=numbers)

    async def inject_malformed_fallback(self, *, pull_number):
        if pull_number == 100:
            return GithubPullRequest.model_validate({})
        return await original_single(self, pull_number=pull_number)

    monkeypatch.setattr(GithubClient, "get_pull_requests_by_numbers", fail_first_multi_lookup)
    monkeypatch.setattr(GithubClient, "get_pull_request", inject_malformed_fallback)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert "leave" in captured.out
    assert "submitted head" in captured.out
    assert "is closed without a result on trunk" in captured.out + captured.err
    assert "last submitted commit is incomplete" in captured.out + captured.err
    assert "could not inspect its current review" in captured.out + captured.err
    assert "invalid data for PR #100" in captured.out + captured.err
    state = state_store.load()
    assert first.change_id in state.review_identities
    assert second.change_id not in state.review_identities
    assert third.change_id in state.review_identities
    assert "incomplete-change" in state.review_identities
    assert set(missing_change_ids) <= state.review_identities.keys()
    assert malformed_change_id in state.review_identities
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[3].state == "closed"
