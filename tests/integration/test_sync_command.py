from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

import jj_stack.commands.sync as sync_command
from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateError, ReviewStateStore, resolve_state_path

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
    write_file,
)
from ..support.submit_property_harness import advance_remote_trunk, update_remote_ref
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


def test_sync_reports_nothing_to_submit_when_whole_stack_merged(
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
    assert "Nothing to submit: everything in this stack has merged." in captured.out
    assert JjClient(repo).resolve_revision("@").only_parent_commit_id() == read_remote_ref(
        fake_repo.git_dir, "main"
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


def test_sync_converges_the_local_stack_after_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.pull_requests[2].is_draft = True
    stack = selected_stack(repo)
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


def test_sync_reports_a_failed_tracking_removal_in_its_exit_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A failed durable write must not look like a clean run to a scripted caller."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    on_trunk, top = selected_stack(repo).revisions
    _squash_merge_pull_request(fake_repo, 1)

    def fail_retire(*_args: object, **_kwargs: object) -> None:
        raise ReviewStateError("Could not write jj-stack data file /x/state.json")

    monkeypatch.setattr(ReviewStateStore, "retire_review", fail_retire)

    exit_code = run_main(repo, config_path, "sync", top.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    unwrapped = " ".join(captured.out.split())
    assert "could not remove tracking" in unwrapped
    assert "jj-stack cleanup" in unwrapped
    # The store's own hint must not be spliced into this line.
    assert "Move the file aside" not in unwrapped
    assert on_trunk.change_id in ReviewStateStore.for_repo(repo).load().review_identities


def test_sync_all_reports_a_failed_tracking_removal_in_its_exit_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Repository-wide recovery computes its own exit status and needs the same rule."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (on_trunk,) = selected_stack(repo).revisions
    # Global recovery acts only on an exact submitted commit reachable from trunk, which a
    # merge commit produces and a squash merge does not.
    fake_repo.apply_merge_commit((fake_repo.pull_requests[1],))
    capsys.readouterr()

    def fail_retire(*_args: object, **_kwargs: object) -> None:
        raise ReviewStateError("Could not write jj-stack data file /x/state.json")

    monkeypatch.setattr(ReviewStateStore, "retire_review", fail_retire)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "could not remove tracking" in " ".join(captured.out.split())
    assert on_trunk.change_id in ReviewStateStore.for_repo(repo).load().review_identities


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
    assert "jj-stack unstack --cleanup" in unwrapped
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


def test_sync_checks_stack_branch_drift_before_rewriting_local_history(
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
    assert JjClient(repo).resolve_revision(on_trunk.change_id).commit_id == on_trunk.commit_id
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == survivor.commit_id
    assert state_store.load() == state_before
    review_temp = JjClient(repo).review_temp_artifacts()
    assert (review_temp.ref_target, review_temp.bookmark_targets) == (None, ())


def test_sync_retries_stack_adoption_after_post_apply_branch_drift(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).revisions
    first_remote_survivor = _simulate_stack_partial_merge(fake_repo)
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
    assert on_trunk.change_id in interrupted_state.review_identities
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
    assert on_trunk.change_id not in recovered_state.review_identities
    assert recovered_state.submitted_baselines[survivor.change_id].commit_id == (
        second_remote_survivor
    )
    assert JjClient(repo).resolve_revision(survivor.change_id).commit_id == (
        second_remote_survivor
    )


def test_sync_all_requires_terminal_stack_merge_for_exact_stack_member(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first, second = selected_stack(repo).revisions
    state_store = ReviewStateStore.for_repo(repo)
    second_baseline = state_store.load().submitted_baselines[second.change_id]
    fake_repo.github_stacks = {7: (1, 2)}
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
    assert "still lists PR #1 as an active member" in " ".join(blocked.err.split())
    assert first.change_id in state_store.load().review_identities
    assert fake_repo.pull_requests[1].state == "open"

    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-23T12:00:00Z"
    fake_repo.pull_requests[1].merge_commit_sha = first.commit_id
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
    assert "still has active members tracked here" in " ".join(applied.err.split())
    assert first.change_id in state_store.load().review_identities

    raw_state["submitted_baselines"][second.change_id] = second_baseline.model_dump(mode="json")
    write_file(state_path, json.dumps(raw_state))
    selected_exit = run_main(repo, config_path, "sync", second.change_id)
    selected = capsys.readouterr()

    assert selected_exit == 0, (selected.out, selected.err)
    assert first.change_id not in state_store.load().review_identities
    assert state_store.load().submitted_baselines[second.change_id].commit_id == remote_survivor
    assert fake_repo.github_stacks == {7: (1, 2)}


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
    assert f"Reviewed {reviewed.change_id[:8]}" in unwrapped
    assert f"unmerged local changes: {lower.change_id[:8]}" in unwrapped
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


def test_sync_preserves_a_described_working_copy_above_the_reviewed_survivor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    on_trunk, reviewed = selected_stack(repo).revisions
    write_file(repo / "local-wip.txt", "keep this work\n")
    run_command(["jj", "describe", "-m", "local WIP"], repo)
    jj = JjClient(repo)
    working_copy_before = jj.resolve_revision("@")
    state_before = ReviewStateStore.for_repo(repo).load()
    reviewed_head_before = fake_repo.pull_requests[2].head_sha
    _squash_merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", reviewed.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Other local changes depend on this stack" in captured.err
    working_copy_after = jj.resolve_revision("@")
    assert working_copy_after.commit_id == working_copy_before.commit_id
    assert working_copy_after.only_parent_commit_id() == reviewed.commit_id
    assert jj.resolve_revision(reviewed.change_id).commit_id == reviewed.commit_id
    assert jj.resolve_revision(on_trunk.change_id).commit_id == on_trunk.commit_id
    assert ReviewStateStore.for_repo(repo).load() == state_before
    assert fake_repo.pull_requests[2].head_sha == reviewed_head_before


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
    assert rewritten_reviewed.only_parent_commit_id() == read_remote_ref(
        fake_repo.git_dir, "main"
    )
    assert rewritten_trailing.only_parent_commit_id() == rewritten_reviewed.commit_id
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
    assert "leave" in captured.err.lower()
    assert "submitted commit is unavailable locally" in captured.err
    state = state_store.load()
    assert first.change_id in state.review_identities
    assert second.change_id not in state.review_identities
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
