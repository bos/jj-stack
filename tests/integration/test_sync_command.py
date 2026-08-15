from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

import jj_stack.commands.sync as sync_command
from jj_stack.errors import EXIT_GITHUB, CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
    write_file,
)
from ..support.submit_property_harness import update_remote_ref
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    remote_refs,
    run_main,
)

# Every case in this file is part of the bounded merge and post-merge convergence
# corpus described in docs/internals/property-testing.md.
pytestmark = pytest.mark.merge_recovery


def _squash_merge_pull_request(fake_repo, pull_number: int) -> None:
    stack_number = fake_repo.stack_number_for_pull(pull_number)
    if stack_number is not None:
        del fake_repo.github_stacks[stack_number]
    fake_repo.apply_squash_merge(fake_repo.pull_requests[pull_number])


def _simulate_stack_partial_merge(fake_repo) -> str:
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.apply_squash_merge(fake_repo.pull_requests[1])
    return fake_repo.rewrite_pull_request_onto_base(
        fake_repo.pull_requests[2],
        base_ref="main",
    )


def _add_other_workspace(repo: Path, root: Path, revision: str) -> None:
    run_command(
        [
            "jj",
            "workspace",
            "add",
            "--name",
            "other",
            "--revision",
            revision,
            str(root),
        ],
        repo,
    )


def test_sync_leaves_a_partially_merged_queued_review_alone(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack_before = selected_stack(repo)
    state_before = ReviewStateStore.for_repo(repo).load()
    top_pull_request = fake_repo.pull_requests[2]
    top_remote_before = fake_repo.ref_target(top_pull_request.head_ref)
    fake_repo.apply_squash_merge(fake_repo.pull_requests[1])
    top_pull_request.is_queued = True

    exit_code = run_main(repo, config_path, "sync", stack_before.head.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "Nothing to sync" in captured.out
    assert fake_repo.ref_target(top_pull_request.head_ref) == top_remote_before
    assert tuple(revision.commit_id for revision in selected_stack(repo).revisions) == tuple(
        revision.commit_id for revision in stack_before.revisions
    )
    assert ReviewStateStore.for_repo(repo).load() == state_before


def test_sync_dry_run_previews_rebase_and_skips_submit_preview(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    top_change_id = stack.revisions[1].change_id
    top_commit_id = stack.revisions[1].commit_id
    original_base_ref = fake_repo.pull_requests[2].base_ref
    _squash_merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", "--dry-run", top_change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would remove merged changes from the bottom" in captured.out
    assert f"jj-stack sync {top_change_id}" in captured.out
    assert "remaining existing PRs" in captured.out
    assert JjClient(repo).resolve_revision(top_change_id).commit_id == top_commit_id
    assert fake_repo.pull_requests[2].base_ref == original_base_ref


def test_sync_finishes_when_whole_stack_merged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _squash_merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert JjClient(repo).resolve_revision("@").parents == (
        read_remote_ref(fake_repo.git_dir, "main"),
    )
    # No replacement pull request was opened for the merged change.
    assert set(fake_repo.pull_requests) == {1}


def test_sync_recovers_a_clean_single_review_rebase_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    reviewed = selected_stack(repo).head
    state_store = ReviewStateStore.for_repo(repo)
    identity = state_store.load().review_identities[reviewed.change_id]
    review_branch = identity.head_ref
    landed_commit_id = fake_repo.apply_rebase_merge(fake_repo.pull_requests[identity.pr_number])

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    copies = JjClient(repo).query_revisions_by_change_ids((reviewed.change_id,))[
        reviewed.change_id
    ]
    assert tuple(item.commit_id for item in copies) == (landed_commit_id,)
    assert copies[0].immutable
    assert JjClient(repo).resolve_revision("@").parents == (landed_commit_id,)
    assert reviewed.change_id not in state_store.load().review_identities
    assert f"refs/heads/{review_branch}" not in remote_refs(fake_repo.git_dir)


def test_sync_all_finds_cross_workspace_recovery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (reviewed,) = selected_stack(repo).revisions
    other_workspace = tmp_path / "other-workspace"
    _add_other_workspace(repo, other_workspace, reviewed.change_id)
    commit_file(other_workspace, "dependent", "dependent.txt")
    run_command(["jj", "edit", "@-"], other_workspace)
    dependent = JjClient(other_workspace).resolve_revision("@")
    state_store = ReviewStateStore.for_repo(repo)
    review_branch = state_store.load().review_identities[reviewed.change_id].head_ref
    _squash_merge_pull_request(fake_repo, 1)
    landed_commit_id = read_remote_ref(fake_repo.git_dir, "main")

    capsys.readouterr()

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert reviewed.change_id not in state_store.load().review_identities
    assert f"refs/heads/{review_branch}" not in remote_refs(fake_repo.git_dir)
    rewritten_dependent = JjClient(other_workspace).resolve_revision("@")
    assert rewritten_dependent.change_id == dependent.change_id
    assert rewritten_dependent.parents == (landed_commit_id,)


def test_sync_all_rebases_a_workspace_child_of_an_exact_merge_side_copy(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (reviewed,) = selected_stack(repo).revisions
    other_workspace = tmp_path / "other-workspace"
    _add_other_workspace(repo, other_workspace, reviewed.change_id)
    commit_file(other_workspace, "dependent", "dependent.txt")
    run_command(["jj", "edit", "@-"], other_workspace)
    dependent = JjClient(other_workspace).resolve_revision("@")
    fake_repo.apply_merge_commit((fake_repo.pull_requests[1],))
    landed_commit_id = read_remote_ref(fake_repo.git_dir, "main")
    run_command(["jj", "git", "fetch"], repo)
    run_command(["jj", "new", "trunk()"], repo)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    rewritten = JjClient(other_workspace).resolve_revision("@")
    assert rewritten.change_id == dependent.change_id
    assert rewritten.parents == (landed_commit_id,)


def test_sync_all_exact_merge_does_not_select_an_unrelated_post_trunk_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (reviewed,) = selected_stack(repo).revisions
    state_store = ReviewStateStore.for_repo(repo)
    review_branch = state_store.load().review_identities[reviewed.change_id].head_ref
    fake_repo.apply_merge_commit((fake_repo.pull_requests[1],))
    run_command(["jj", "git", "fetch"], repo)
    run_command(["jj", "new", "trunk()"], repo)
    commit_file(repo, "unrelated", "unrelated.txt")
    unrelated = JjClient(repo).resolve_revision("@")
    unrelated_snapshot = (unrelated.commit_id, unrelated.parents)
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    unchanged = JjClient(repo).resolve_revision(unrelated.change_id)
    assert (unchanged.commit_id, unchanged.parents) == unrelated_snapshot
    assert reviewed.change_id not in state_store.load().review_identities
    assert f"refs/heads/{review_branch}" not in remote_refs(fake_repo.git_dir)


def test_sync_all_cleans_a_rewritten_merge_after_its_local_copy_is_gone(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (reviewed,) = selected_stack(repo).revisions
    state_store = ReviewStateStore.for_repo(repo)
    review_branch = state_store.load().review_identities[reviewed.change_id].head_ref
    _squash_merge_pull_request(fake_repo, 1)
    run_command(["jj", "abandon", reviewed.change_id], repo)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert reviewed.change_id not in state_store.load().review_identities
    assert f"refs/heads/{review_branch}" not in remote_refs(fake_repo.git_dir)


def test_sync_all_stops_before_removing_a_change_checked_out_in_another_workspace(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (reviewed,) = selected_stack(repo).revisions
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "independent", "independent.txt")
    independent = selected_stack(repo).head
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    other_workspace = tmp_path / "other-workspace"
    _add_other_workspace(repo, other_workspace, reviewed.change_id)
    run_command(["jj", "edit", reviewed.change_id], other_workspace)
    _squash_merge_pull_request(fake_repo, 1)
    _squash_merge_pull_request(fake_repo, 2)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert reviewed.change_id[:8] in captured.err
    assert "other" in captured.err
    assert str(other_workspace) in captured.err
    assert "jj new" in captured.err
    assert "jj workspace forget" in captured.err
    assert "trash" in captured.err
    assert JjClient(other_workspace).resolve_revision("@").change_id == reviewed.change_id
    remaining = ReviewStateStore.for_repo(repo).load().review_identities
    assert reviewed.change_id in remaining
    assert independent.change_id not in remaining


def test_sync_all_preserves_tracking_when_exact_pr_head_changed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (reviewed,) = selected_stack(repo).revisions
    state_store = ReviewStateStore.for_repo(repo)
    pull_request = fake_repo.pull_requests[1]
    fake_repo.apply_merge_commit((pull_request,))
    fake_repo.force_push_pull_request_head(pull_request)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PR #1" in captured.err
    assert "submitted head" in captured.err
    assert reviewed.change_id in state_store.load().review_identities


def test_sync_all_reports_batch_pull_request_failure_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    async def fail_batch_lookup(self, *, pull_numbers):
        raise GithubClientError("GitHub pull request batch lookup failed: unavailable")

    monkeypatch.setattr(GithubClient, "get_pull_requests_by_numbers", fail_batch_lookup)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == EXIT_GITHUB
    assert "Could not inspect pull requests" in captured.err
    assert "unavailable" in captured.err
    assert "Traceback" not in captured.err


def test_sync_converges_stack_history_and_adopts_rewritten_survivor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).revisions
    remote_survivor = _simulate_stack_partial_merge(fake_repo)

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    state = state_store.load()
    assert on_trunk.change_id not in state.review_identities
    rewritten_survivor = JjClient(repo).resolve_revision(survivor.change_id)
    assert rewritten_survivor.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert JjClient(repo).resolve_revision("@").parents == (rewritten_survivor.commit_id,)
    review_temp = JjClient(repo).review_temp_artifacts()
    assert (review_temp.ref_target, review_temp.bookmark_targets) == (None, ())
    assert state.submitted_baselines[survivor.change_id].commit_id == (
        rewritten_survivor.commit_id
    )
    assert fake_repo.pull_requests[2].head_sha == rewritten_survivor.commit_id
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.github_stacks == {7: (1, 2)}
    on_trunk_versions = JjClient(repo).query_revisions_by_change_ids((on_trunk.change_id,))[
        on_trunk.change_id
    ]
    assert on_trunk_versions == ()
    assert remote_survivor != survivor.commit_id

    survivor_baseline = state_store.load().submitted_baselines[survivor.change_id]
    drifted_head = fake_repo.force_push_pull_request_head(fake_repo.pull_requests[2])
    retry_exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    retry = capsys.readouterr()

    assert retry_exit_code == 1
    assert "none of its merged members is tracked here" in retry.err
    assert state_store.load().submitted_baselines[survivor.change_id] == survivor_baseline
    assert fake_repo.pull_requests[2].head_sha == drifted_head


def test_sync_noop_after_partial_merge_does_not_read_review_refs_or_submit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _, survivor = selected_stack(repo).revisions
    _simulate_stack_partial_merge(fake_repo)
    first_exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    first = capsys.readouterr()
    assert first_exit_code == 0, (first.out, first.err)

    survivor = selected_stack(repo).head
    pull_request_before = deepcopy(fake_repo.pull_requests[2])

    def fail_review_ref_read(*_args, **_kwargs):
        raise AssertionError("no-op sync should not read exact review refs")

    monkeypatch.setattr(JjClient, "list_remote_branches", fail_review_ref_read)
    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "No merged changes in this stack need rebasing." in captured.out
    assert "Submitted changes:" not in captured.out
    assert fake_repo.pull_requests[2] == pull_request_before


def test_sync_preserves_unpublished_edits_to_an_active_stack_survivor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).revisions
    _simulate_stack_partial_merge(fake_repo)
    state_before = state_store.load()
    run_command(["jj", "edit", survivor.change_id], repo)
    write_file(repo / "local-survivor-edit.txt", "keep this edit\n")
    run_command(["jj", "new"], repo)
    edited_survivor = JjClient(repo).resolve_revision(survivor.change_id)

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unpublished local edits" in captured.err
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == (
        edited_survivor.commit_id
    )
    assert JjClient(repo).resolve_revision(on_trunk.change_id).commit_id == on_trunk.commit_id
    assert state_store.load() == state_before


def test_sync_rebases_a_conflicted_review_before_stopping_its_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    on_trunk, reviewed = selected_stack(repo).revisions
    reviewed_baseline = state_store.load().submitted_baselines[reviewed.change_id].commit_id

    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "left conflict", "conflict.txt")
    left = JjClient(repo).resolve_revision("@-")
    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "right conflict", "conflict.txt")
    right = JjClient(repo).resolve_revision("@-")
    run_command(["jj", "new", left.commit_id, right.commit_id], repo)
    conflict_source = JjClient(repo).resolve_revision("@")
    assert conflict_source.conflict
    run_command(
        [
            "jj",
            "restore",
            "--from",
            conflict_source.commit_id,
            "--into",
            reviewed.change_id,
            "conflict.txt",
        ],
        repo,
    )
    run_command(["jj", "edit", reviewed.change_id], repo)
    run_command(["jj", "new"], repo)
    run_command(
        ["jj", "abandon", conflict_source.commit_id, left.commit_id, right.commit_id],
        repo,
    )
    conflicted_before = JjClient(repo).resolve_revision(reviewed.change_id)
    assert conflicted_before.conflict
    _squash_merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 3
    rendered = " ".join(captured.err.split())
    assert "The local rebase is complete" in rendered
    assert f"jj-stack submit {reviewed.change_id}" in rendered
    conflicted_after = JjClient(repo).resolve_revision(reviewed.change_id)
    assert conflicted_after.conflict
    assert conflicted_after.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert conflicted_after.commit_id != conflicted_before.commit_id
    assert fake_repo.pull_requests[2].head_sha == reviewed_baseline
    assert on_trunk.change_id in state_store.load().review_identities


def test_sync_reports_a_closed_stack_survivor_as_a_closed_review_not_branch_drift(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A closed active member is still a survivor, so its branch must not take the blame."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).revisions
    _simulate_stack_partial_merge(fake_repo)
    fake_repo.pull_requests[2].state = "closed"
    state_before = state_store.load()

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    unwrapped = " ".join(captured.err.split())
    assert "PR #2" in unwrapped and "is closed, so sync cannot update that review" in unwrapped
    assert "jj-stack cleanup" in unwrapped
    assert JjClient(repo).resolve_revision(on_trunk.change_id).commit_id == on_trunk.commit_id
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == survivor.commit_id
    assert state_store.load() == state_before


def test_sync_retries_stack_adoption_after_survivor_submit_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).revisions
    baseline_before = state_store.load().submitted_baselines[survivor.change_id]
    remote_survivor = _simulate_stack_partial_merge(fake_repo)
    real_run_submit = sync_command.run_submit_async

    async def fail_submit(**_kwargs):
        raise CliError("injected survivor submit failure")

    monkeypatch.setattr(sync_command, "run_submit_async", fail_submit)
    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    failed = capsys.readouterr()

    assert exit_code == 1
    assert "injected survivor submit failure" in failed.err
    interrupted_state = state_store.load()
    assert on_trunk.change_id in interrupted_state.review_identities
    assert interrupted_state.submitted_baselines[survivor.change_id].commit_id == remote_survivor
    assert remote_survivor != baseline_before.commit_id
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == remote_survivor

    monkeypatch.setattr(sync_command, "run_submit_async", real_run_submit)
    retry_exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    retry = capsys.readouterr()

    assert retry_exit_code == 0, (retry.out, retry.err)
    recovered_state = state_store.load()
    assert on_trunk.change_id not in recovered_state.review_identities
    assert recovered_state.submitted_baselines[survivor.change_id].commit_id == remote_survivor


def test_sync_all_requires_terminal_stack_merge_for_exact_stack_member(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first, second = selected_stack(repo).revisions
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.auto_merge_reachable_heads = False
    update_remote_ref(fake_repo, branch="main", target=first.commit_id)

    selected_exit = run_main(repo, config_path, "sync", second.change_id)
    selected = capsys.readouterr()

    assert selected_exit == 1
    assert "keeps #1 active outside the selected stack" in selected.err
    assert "jj-stack unstack --stack 7" in selected.err
    assert first.change_id in state_store.load().review_identities
    assert fake_repo.pull_requests[1].state == "open"

    blocked_exit = run_main(repo, config_path, "sync", "--all")
    blocked = capsys.readouterr()

    assert blocked_exit == 1
    assert "GitHub still lists PR #1 as an active member" in " ".join(blocked.err.split())
    assert first.change_id in state_store.load().review_identities
    assert fake_repo.pull_requests[1].state == "open"


def test_sync_does_not_trust_active_stack_head_drift_without_merged_history(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    _first, second = selected_stack(repo).revisions
    baseline = state_store.load().submitted_baselines[second.change_id]
    fake_repo.github_stacks = {7: (1, 2)}
    drifted_head = fake_repo.force_push_pull_request_head(fake_repo.pull_requests[2])

    exit_code = run_main(repo, config_path, "sync", second.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "none of its merged members is tracked here" in captured.err
    assert state_store.load().submitted_baselines[second.change_id] == baseline
    assert fake_repo.pull_requests[2].head_sha == drifted_head


def test_sync_restores_change_ids_after_an_exact_github_stack_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    original_reviews = selected_stack(repo).revisions
    original_state = state_store.load()
    commit_file(repo, "local trailing work", "local-trailing.txt")
    original = selected_stack(repo).revisions
    fake_repo.github_stacks = {7: (1, 2)}
    trunk = fake_repo.advance_branch(
        "main",
        path="github-stack-rebase-trunk.txt",
        contents="new trunk contents\n",
    )
    github_heads = fake_repo.rebase_stack_onto_base(7, base_ref="main")
    changed_head = fake_repo.replace_pull_request_head_contents(
        fake_repo.pull_requests[2],
        path="github-only-edit.txt",
        contents="not in the submitted stack\n",
    )

    rejected_exit = run_main(repo, config_path, "sync", original[-1].change_id)
    rejected = capsys.readouterr()

    assert rejected_exit == 1
    assert "does not have the same contents" in rejected.err
    assert state_store.load() == original_state
    assert tuple(
        JjClient(repo).resolve_revision(revision.change_id).commit_id for revision in original
    ) == tuple(revision.commit_id for revision in original)
    assert fake_repo.ref_target(fake_repo.pull_requests[2].head_ref) == changed_head

    update_remote_ref(
        fake_repo,
        branch=fake_repo.pull_requests[2].head_ref,
        target=github_heads[1],
    )
    real_relink_reviews = ReviewStateStore.relink_reviews

    def fail_relink_reviews(self, *, replacements):
        raise CliError("injected tracking update failure")

    monkeypatch.setattr(ReviewStateStore, "relink_reviews", fail_relink_reviews)
    interrupted_exit = run_main(repo, config_path, "sync", original[-1].change_id)
    interrupted = capsys.readouterr()

    assert interrupted_exit == 1
    assert "injected tracking update failure" in interrupted.err
    interrupted_rebase = tuple(
        JjClient(repo).resolve_revision(revision.change_id) for revision in original
    )
    assert tuple(
        fake_repo.ref_target(fake_repo.pull_requests[index].head_ref) for index in (1, 2)
    ) == tuple(revision.commit_id for revision in interrupted_rebase[:2])
    assert state_store.load() == original_state

    monkeypatch.setattr(ReviewStateStore, "relink_reviews", real_relink_reviews)
    exit_code = run_main(repo, config_path, "sync", original[-1].change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "Restoring the stack's jj change IDs" in captured.out
    rewritten = tuple(
        JjClient(repo).resolve_revision(revision.change_id) for revision in original
    )
    assert tuple(revision.change_id for revision in rewritten) == tuple(
        revision.change_id for revision in original
    )
    assert rewritten[0].parents == (trunk,)
    assert rewritten[1].parents == (rewritten[0].commit_id,)
    assert rewritten[2].parents == (rewritten[1].commit_id,)
    assert tuple(
        fake_repo.ref_target(fake_repo.pull_requests[index].head_ref) for index in (1, 2)
    ) == tuple(revision.commit_id for revision in rewritten[:2])
    assert tuple(
        state_store.load().submitted_baselines[revision.change_id].commit_id
        for revision in original_reviews
    ) == tuple(revision.commit_id for revision in rewritten[:2])
    assert tuple(revision.commit_id for revision in rewritten[:2]) != github_heads
    assert (repo / "local-trailing.txt").read_text() == "local trailing work\n"
    assert (repo / "github-stack-rebase-trunk.txt").read_text() == "new trunk contents\n"
    assert JjClient(repo).review_temp_artifacts().ref_target is None


def test_sync_rejects_a_reviewed_unreviewed_reviewed_sandwich_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    on_trunk, reviewed = stack.revisions
    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "local middle", "local-middle.txt")
    local_middle = selected_stack(repo).head
    run_command(["jj", "rebase", "-r", reviewed.change_id, "-d", local_middle.change_id], repo)
    reviewed_before = JjClient(repo).resolve_revision(reviewed.change_id).commit_id
    middle_before = JjClient(repo).resolve_revision(local_middle.change_id).commit_id
    _squash_merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "appears above an unreviewed change" in captured.err
    assert JjClient(repo).resolve_revision(reviewed.change_id).commit_id == reviewed_before
    assert JjClient(repo).resolve_revision(local_middle.change_id).commit_id == middle_before
    assert set(fake_repo.pull_requests) == {1, 2}


def test_sync_explains_the_reported_rebase_ordering_stop_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    initial = selected_stack(repo)
    reviewed = initial.head
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    identity = initial_state.review_identities[reviewed.change_id]
    submitted_commit_id = initial_state.submitted_baselines[reviewed.change_id].commit_id
    run_command(["jj", "new", initial.base_parent.commit_id], repo)
    commit_file(repo, "local lower", "local-lower.txt")
    lower = selected_stack(repo).head
    run_command(["jj", "rebase", "-r", reviewed.commit_id, "-o", lower.commit_id], repo)
    local_reviewed = JjClient(repo).resolve_revision(reviewed.change_id)
    run_command(["jj", "new", local_reviewed.commit_id], repo)
    landed_commit_id = fake_repo.apply_rebase_merge(fake_repo.pull_requests[identity.pr_number])
    JjClient(repo).fetch_remote(remote="origin")

    change_exit = run_main(repo, config_path, "view", reviewed.change_id)
    change_view = capsys.readouterr()
    pr_exit = run_main(
        repo,
        config_path,
        "view",
        "--pull-request",
        str(identity.pr_number),
    )
    pr_view = capsys.readouterr()

    assert change_exit == 0
    assert pr_exit == 0
    assert "local lower" in change_view.out and "feature 1" in change_view.out
    assert "local lower" in pr_view.out and "feature 1" in pr_view.out
    jj = JjClient(repo)
    dag_before = {item.commit_id: item for item in jj.query_revisions("visible()")}
    state_before = state_store.load()
    refs_before = remote_refs(fake_repo.git_dir)
    pull_requests_before = deepcopy(fake_repo.pull_requests)
    reviews_before = deepcopy(fake_repo.pull_request_reviews)
    events_before = deepcopy(fake_repo.pull_request_events)

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    unwrapped = " ".join(captured.err.split())
    assert f"Cannot sync reviewed {reviewed.change_id[:8]}" in unwrapped
    assert f"unmerged local changes are its parents: {lower.change_id[:8]}" in unwrapped
    assert "cannot decide whether those local changes belong before or after it" in unwrapped
    assert f"Submitted commit: {submitted_commit_id}" in unwrapped
    assert f"Local copy commit: {local_reviewed.commit_id}" in unwrapped
    assert f"Fetched trunk commit: {landed_commit_id}" in unwrapped
    assert f"jj log -r 'trunk() | (trunk()..{local_reviewed.commit_id})'" in unwrapped
    assert "ask an agent to inspect this repository and these commit IDs" in unwrapped
    assert "jj-stack view" in unwrapped
    assert "jj-stack sync <head-change-id>" in unwrapped
    assert "jj-stack cleanup" in unwrapped
    assert {item.commit_id: item for item in jj.query_revisions("visible()")} == dag_before
    assert state_store.load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before
    assert fake_repo.pull_requests == pull_requests_before
    assert fake_repo.pull_request_reviews == reviews_before
    assert fake_repo.pull_request_events == events_before


def test_sync_converges_selected_path_while_a_sibling_still_needs_the_merged_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    on_trunk, reviewed = selected_stack(repo).revisions
    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "sibling work", "sibling.txt")
    sibling = selected_stack(repo).head
    jj = JjClient(repo)
    _squash_merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "PR #1" in captured.out
    assert f"jj-stack sync {sibling.change_id[:8]}" in captured.out
    assert f"jj-stack sync {sibling.change_id}" not in captured.out
    rewritten_reviewed = jj.resolve_revision(reviewed.change_id)
    assert rewritten_reviewed.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert fake_repo.pull_requests[2].head_sha == rewritten_reviewed.commit_id
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert jj.resolve_revision(sibling.change_id).parents == (on_trunk.commit_id,)
    assert jj.resolve_revision(on_trunk.change_id).commit_id == on_trunk.commit_id
    assert on_trunk.change_id in ReviewStateStore.for_repo(repo).load().review_identities


def test_sync_rebases_trailing_local_work_without_creating_a_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    initial = selected_stack(repo)
    on_trunk, reviewed = initial.revisions
    commit_file(repo, "local trailing", "local-trailing.txt")
    trailing = selected_stack(repo).head
    state_before = ReviewStateStore.for_repo(repo).load()
    assert trailing.change_id not in state_before.review_identities
    _squash_merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", trailing.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    jj = JjClient(repo)
    rewritten_reviewed = jj.resolve_revision(reviewed.change_id)
    rewritten_trailing = jj.resolve_revision(trailing.change_id)
    assert rewritten_reviewed.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert rewritten_trailing.parents == (rewritten_reviewed.commit_id,)
    assert set(fake_repo.pull_requests) == {1, 2}
    assert trailing.change_id not in ReviewStateStore.for_repo(repo).load().review_identities


def test_sync_requires_every_surviving_review_before_rewriting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    reviewed = stack.revisions[1]
    reviewed_before = reviewed.commit_id
    _squash_merge_pull_request(fake_repo, 1)
    del fake_repo.pull_requests[2]

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no longer reports PR #2" in captured.err
    assert JjClient(repo).resolve_revision(reviewed.change_id).commit_id == reviewed_before
    assert set(fake_repo.pull_requests) == {1}


def test_sync_all_isolates_an_unavailable_snapshot_from_an_exact_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first, second = selected_stack(repo).revisions
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    git_dir = str(fake_repo.git_dir)
    remote_tree = run_command(
        ["git", "--git-dir", git_dir, "rev-parse", f"{first.commit_id}^{{tree}}"],
        fake_repo.git_dir.parent,
    ).stdout.strip()
    unavailable_commit_id = run_command(
        [
            "git",
            "-c",
            "user.name=External User",
            "-c",
            "user.email=external@example.com",
            "--git-dir",
            git_dir,
            "commit-tree",
            remote_tree,
            "-p",
            first.commit_id,
            "-m",
            "external review head",
        ],
        fake_repo.git_dir.parent,
    ).stdout.strip()
    update_remote_ref(
        fake_repo,
        branch=initial_state.review_identities[first.change_id].head_ref,
        target=unavailable_commit_id,
    )
    state_path = resolve_state_path(repo)
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["submitted_baselines"][first.change_id]["commit_id"] = unavailable_commit_id
    write_file(state_path, json.dumps(raw_state))

    fake_repo.auto_merge_reachable_heads = False
    fake_repo.github_stacks = {}
    update_remote_ref(fake_repo, branch="main", target=second.commit_id)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert "PR #1" in captured.err
    assert first.change_id[:8] in captured.err
    assert "submitted commit is unavailable locally" in captured.err
    state = state_store.load()
    assert first.change_id in state.review_identities
    assert second.change_id not in state.review_identities
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
