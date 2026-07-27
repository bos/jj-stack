from __future__ import annotations

import json
from pathlib import Path

import pytest

import jj_stack.commands.sync as sync_command
from jj_stack.errors import EXIT_USAGE, CliError
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
from ..support.submit_property_harness import advance_remote_trunk, update_remote_ref
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    run_main,
)


def _merge_pull_request(fake_repo, pull_number: int) -> None:
    fake_repo.apply_squash_merge(fake_repo.pull_requests[pull_number])


def _simulate_native_partial_merge(fake_repo) -> str:
    fake_repo.native_stacks = {7: (1, 2)}
    fake_repo.apply_squash_merge(fake_repo.pull_requests[1])
    return fake_repo.rewrite_pull_request_onto_base(
        fake_repo.pull_requests[2],
        base_ref="main",
    )


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
    assert "Would remove merged changes from the bottom" in captured.out
    assert f"jj-stack sync {top_change_id}" in captured.out
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
    assert "Nothing to submit: everything in this stack has merged." in captured.out
    assert JjClient(repo).resolve_revision("@").only_parent_commit_id() == read_remote_ref(
        fake_repo.git_dir, "main"
    )
    # No replacement pull request was opened for the merged change.
    assert set(fake_repo.pull_requests) == {1}


def test_sync_converges_the_local_stack_after_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.pull_requests[2].is_draft = True
    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[1].change_id
    top_commit_id = stack.revisions[1].commit_id

    merge_exit_code = run_main(repo, config_path, "merge")
    capsys.readouterr()
    assert merge_exit_code == 0
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "open"
    assert JjClient(repo).resolve_revision(top_change_id).commit_id == top_commit_id

    sync_exit_code = run_main(repo, config_path, "sync", top_change_id)
    captured = capsys.readouterr()

    assert sync_exit_code == 0, (captured.out, captured.err)
    merged_trunk_commit = read_remote_ref(fake_repo.git_dir, "main")
    rewritten_top = JjClient(repo).resolve_revision(top_change_id)
    assert rewritten_top.only_parent_commit_id() == merged_trunk_commit
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[2].state == "open"


@pytest.mark.landing_recovery
def test_sync_reports_a_failed_tracking_removal_in_its_exit_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A failed durable write must not look like a clean run to a scripted caller."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.pull_requests[2].is_draft = True
    landed, top = JjClient(repo).discover_review_stack().revisions
    assert run_main(repo, config_path, "merge") == 0
    capsys.readouterr()

    def fail_retire(*_args: object, **_kwargs: object) -> None:
        raise OSError("state file is read-only")

    monkeypatch.setattr(ReviewStateStore, "retire_review", fail_retire)

    exit_code = run_main(repo, config_path, "sync", top.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    unwrapped = " ".join(captured.out.split())
    assert "could not remove tracking" in unwrapped
    assert "jj-stack cleanup" in unwrapped
    assert landed.change_id in ReviewStateStore.for_repo(repo).load().review_identities


@pytest.mark.landing_recovery
def test_sync_all_reports_a_failed_tracking_removal_in_its_exit_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Repository-wide recovery computes its own exit status and needs the same rule."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (landed,) = JjClient(repo).discover_review_stack().revisions
    # Global recovery acts only on an exact submitted commit reachable from trunk. The fake's
    # merge endpoint always squashes, so land the exact commit on trunk directly instead.
    update_remote_ref(fake_repo, branch="main", target=landed.commit_id)
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-26T12:00:00Z"
    capsys.readouterr()

    def fail_retire(*_args: object, **_kwargs: object) -> None:
        raise OSError("state file is read-only")

    monkeypatch.setattr(ReviewStateStore, "retire_review", fail_retire)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "could not remove tracking" in " ".join(captured.out.split())
    assert landed.change_id in ReviewStateStore.for_repo(repo).load().review_identities


@pytest.mark.landing_recovery
def test_sync_converges_native_history_and_adopts_rewritten_survivor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    landed, survivor = JjClient(repo).discover_review_stack().revisions
    remote_survivor = _simulate_native_partial_merge(fake_repo)

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    state = state_store.load()
    assert landed.change_id not in state.review_identities
    rewritten_survivor = JjClient(repo).resolve_revision(survivor.change_id)
    assert rewritten_survivor.only_parent_commit_id() == read_remote_ref(
        fake_repo.git_dir,
        "main",
    )
    assert JjClient(repo).resolve_revision("@").only_parent_commit_id() == (
        rewritten_survivor.commit_id
    )
    review_temp = JjClient(repo).review_temp_artifacts()
    assert (review_temp.ref_target, review_temp.bookmark_targets) == (None, ())
    assert state.submitted_baselines[survivor.change_id].commit_id == (
        rewritten_survivor.commit_id
    )
    assert fake_repo.pull_requests[2].head_sha == rewritten_survivor.commit_id
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.native_stacks == {7: (1, 2)}
    landed_versions = JjClient(repo).query_revisions_by_change_ids((landed.change_id,))[
        landed.change_id
    ]
    assert landed_versions == ()
    assert remote_survivor != survivor.commit_id

    survivor_baseline = state_store.load().submitted_baselines[survivor.change_id]
    drifted_head = fake_repo.force_push_pull_request_head(fake_repo.pull_requests[2])
    retry_exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    retry = capsys.readouterr()

    assert retry_exit_code == 1
    assert "changed externally" in retry.err
    assert state_store.load().submitted_baselines[survivor.change_id] == survivor_baseline
    assert fake_repo.pull_requests[2].head_sha == drifted_head


@pytest.mark.landing_recovery
def test_sync_preserves_unpublished_edits_to_an_active_native_survivor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    landed, survivor = JjClient(repo).discover_review_stack().revisions
    _simulate_native_partial_merge(fake_repo)
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
    assert JjClient(repo).resolve_revision(landed.change_id).commit_id == landed.commit_id
    assert state_store.load() == state_before


@pytest.mark.landing_recovery
def test_sync_reports_a_closed_native_survivor_as_a_closed_review_not_branch_drift(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A closed active member is still a survivor, so its branch must not take the blame."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    landed, survivor = JjClient(repo).discover_review_stack().revisions
    _simulate_native_partial_merge(fake_repo)
    fake_repo.pull_requests[2].state = "closed"
    state_before = state_store.load()

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    unwrapped = " ".join(captured.err.split())
    assert "PR #2" in unwrapped and "is closed, so sync cannot update that review" in unwrapped
    assert f"jj-stack submit --restart {survivor.change_id}" in unwrapped
    assert JjClient(repo).resolve_revision(landed.change_id).commit_id == landed.commit_id
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == survivor.commit_id
    assert state_store.load() == state_before


@pytest.mark.landing_recovery
def test_sync_retries_native_adoption_after_survivor_submit_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    landed, survivor = JjClient(repo).discover_review_stack().revisions
    baseline_before = state_store.load().submitted_baselines[survivor.change_id]
    remote_survivor = _simulate_native_partial_merge(fake_repo)
    real_run_submit = sync_command.run_submit_async

    async def fail_submit(**_kwargs):
        raise CliError("injected survivor submit failure")

    monkeypatch.setattr(sync_command, "run_submit_async", fail_submit)
    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    failed = capsys.readouterr()

    assert exit_code == 1
    assert "injected survivor submit failure" in failed.err
    interrupted_state = state_store.load()
    assert landed.change_id in interrupted_state.review_identities
    assert interrupted_state.submitted_baselines[survivor.change_id].commit_id == remote_survivor
    assert remote_survivor != baseline_before.commit_id
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == remote_survivor

    monkeypatch.setattr(sync_command, "run_submit_async", real_run_submit)
    retry_exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    retry = capsys.readouterr()

    assert retry_exit_code == 0, (retry.out, retry.err)
    recovered_state = state_store.load()
    assert landed.change_id not in recovered_state.review_identities
    assert recovered_state.submitted_baselines[survivor.change_id].commit_id == remote_survivor


@pytest.mark.landing_recovery
def test_sync_checks_native_branch_drift_before_rewriting_local_history(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    landed, survivor = JjClient(repo).discover_review_stack().revisions
    _simulate_native_partial_merge(fake_repo)
    state_before = state_store.load()
    require_targets = JjClient._require_remote_branch_targets_at_url
    drifted = False

    def drift_before_apply(self, *, fetch_url, expected_targets):
        nonlocal drifted
        if not drifted:
            drifted = True
            update_remote_ref(
                fake_repo,
                branch=fake_repo.pull_requests[2].head_ref,
                target=survivor.commit_id,
            )
        return require_targets(
            self,
            fetch_url=fetch_url,
            expected_targets=expected_targets,
        )

    monkeypatch.setattr(
        JjClient,
        "_require_remote_branch_targets_at_url",
        drift_before_apply,
    )
    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no longer points to the expected commit" in captured.err
    assert JjClient(repo).resolve_revision(landed.change_id).commit_id == landed.commit_id
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == survivor.commit_id
    assert state_store.load() == state_before
    review_temp = JjClient(repo).review_temp_artifacts()
    assert (review_temp.ref_target, review_temp.bookmark_targets) == (None, ())


@pytest.mark.landing_recovery
def test_sync_retries_native_adoption_after_post_apply_branch_drift(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    landed, survivor = JjClient(repo).discover_review_stack().revisions
    first_remote_survivor = _simulate_native_partial_merge(fake_repo)
    require_targets = JjClient._require_remote_branch_targets_at_url
    checks = 0
    second_remote_survivor: str | None = None

    def drift_after_apply(self, *, fetch_url, expected_targets):
        nonlocal checks, second_remote_survivor
        checks += 1
        if checks == 2:
            second_remote_survivor = fake_repo.rewrite_pull_request_onto_base(
                fake_repo.pull_requests[2],
                base_ref="main",
            )
        return require_targets(
            self,
            fetch_url=fetch_url,
            expected_targets=expected_targets,
        )

    monkeypatch.setattr(
        JjClient,
        "_require_remote_branch_targets_at_url",
        drift_after_apply,
    )
    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    failed = capsys.readouterr()

    assert exit_code == 1
    assert "no longer points to the expected commit" in failed.err
    assert second_remote_survivor is not None
    interrupted_state = state_store.load()
    assert landed.change_id in interrupted_state.review_identities
    assert interrupted_state.submitted_baselines[survivor.change_id].commit_id == (
        first_remote_survivor
    )
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == (
        first_remote_survivor
    )
    review_temp = JjClient(repo).review_temp_artifacts()
    assert (review_temp.ref_target, review_temp.bookmark_targets) == (None, ())

    monkeypatch.setattr(
        JjClient,
        "_require_remote_branch_targets_at_url",
        require_targets,
    )
    retry_exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    retry = capsys.readouterr()

    assert retry_exit_code == 0, (retry.out, retry.err)
    recovered_state = state_store.load()
    assert landed.change_id not in recovered_state.review_identities
    assert recovered_state.submitted_baselines[survivor.change_id].commit_id == (
        second_remote_survivor
    )
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == (
        second_remote_survivor
    )


@pytest.mark.landing_recovery
def test_sync_all_requires_terminal_merge_for_exact_native_member(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first, second = JjClient(repo).discover_review_stack().revisions
    state_store = ReviewStateStore.for_repo(repo)
    second_baseline = state_store.load().submitted_baselines[second.change_id]
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    fake_repo.native_stacks = {7: (1, 2)}
    fake_repo.auto_merge_reachable_heads = False
    update_remote_ref(fake_repo, branch="main", target=first.commit_id)

    selected_exit = run_main(repo, config_path, "sync", second.change_id)
    selected = capsys.readouterr()

    assert selected_exit == 1
    assert "keeps #1 active outside the selected stack" in selected.err
    assert "gh stack unstack 7" in selected.err
    assert first.change_id in state_store.load().review_identities
    assert fake_repo.pull_requests[1].state == "open"

    blocked_exit = run_main(repo, config_path, "sync", "--all")
    blocked = capsys.readouterr()

    assert blocked_exit == 1
    assert "not terminally merged" in blocked.err
    assert first.change_id in state_store.load().review_identities
    assert fake_repo.pull_requests[1].state == "open"

    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-23T12:00:00Z"
    fake_repo.pull_requests[1].merge_commit_sha = first.commit_id
    fake_repo.native_stacks = {7: (1, 2), 8: (1,)}
    ambiguous_exit = run_main(repo, config_path, "sync", "--all")
    ambiguous = capsys.readouterr()

    assert ambiguous_exit == 1
    assert "ambiguous membership" in ambiguous.err
    assert first.change_id in state_store.load().review_identities

    fake_repo.native_stacks = {7: (1, 2)}
    advance_remote_trunk(fake_repo)
    remote_survivor = fake_repo.rewrite_pull_request_onto_base(
        fake_repo.pull_requests[2],
        base_ref="main",
    )
    state_path = resolve_state_path(repo)
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["submitted_baselines"].pop(second.change_id)
    write_file(state_path, json.dumps(raw_state))
    applied_exit = run_main(repo, config_path, "sync", "--all")
    applied = capsys.readouterr()

    assert applied_exit == 1
    assert "tracked active members" in applied.err
    assert first.change_id in state_store.load().review_identities

    raw_state["submitted_baselines"][second.change_id] = second_baseline.model_dump(mode="json")
    write_file(state_path, json.dumps(raw_state))
    selected_exit = run_main(repo, config_path, "sync", second.change_id)
    selected = capsys.readouterr()

    assert selected_exit == 0, (selected.out, selected.err)
    assert first.change_id not in state_store.load().review_identities
    assert state_store.load().submitted_baselines[second.change_id].commit_id == remote_survivor
    assert fake_repo.native_stacks == {7: (1, 2)}


@pytest.mark.landing_recovery
def test_sync_does_not_trust_active_native_head_drift_without_merged_history(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("github.test/octo-org/stacked-review", True)
    _first, second = JjClient(repo).discover_review_stack().revisions
    baseline = state_store.load().submitted_baselines[second.change_id]
    fake_repo.native_stacks = {7: (1, 2)}
    drifted_head = fake_repo.force_push_pull_request_head(fake_repo.pull_requests[2])

    exit_code = run_main(repo, config_path, "sync", second.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "changed externally" in captured.err
    assert state_store.load().submitted_baselines[second.change_id] == baseline
    assert fake_repo.pull_requests[2].head_sha == drifted_head


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
        .model_copy(update={"head_ref": "review/incomplete-incomple", "pr_number": 99})
        .model_dump(mode="json")
    )
    missing_change_ids = tuple(f"miss{index:04d}change" for index in range(64))
    for index, change_id in enumerate(missing_change_ids, start=100):
        raw_state["review_identities"][change_id] = (
            initial_state.review_identities[first.change_id]
            .model_copy(
                update={
                    "head_ref": f"review/missing-{change_id[:8]}",
                    "pr_number": index,
                }
            )
            .model_dump(mode="json")
        )
        raw_state["submitted_baselines"][change_id] = (
            initial_state.submitted_baselines[first.change_id]
            .model_copy(update={"commit_id": f"{index:040x}"})
            .model_dump(mode="json")
        )
    malformed_change_id = "malformedbaseline"
    raw_state["review_identities"][malformed_change_id] = (
        initial_state.review_identities[first.change_id]
        .model_copy(update={"head_ref": "review/malformed-malforme", "pr_number": 164})
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
