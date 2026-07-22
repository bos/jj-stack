from __future__ import annotations

import json
from pathlib import Path

from jj_stack.formatting import short_change_id
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    write_file,
)
from .submit_command_helpers import configure_submit_environment, run_main


def test_restart_prepares_submitted_stack_for_fresh_pull_requests(
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
    head_change_id = stack.head.change_id
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    old_bookmarks = {
        change_id: initial_state.review_identities[change_id].head_ref for change_id in change_ids
    }
    old_pr_numbers = {
        change_id: initial_state.review_identities[change_id].pr_number
        for change_id in change_ids
    }
    for pr_number in old_pr_numbers.values():
        assert pr_number is not None
        fake_repo.pull_requests[pr_number].state = "closed"

    exit_code = run_main(repo, config_path, "restart", head_change_id)
    captured = capsys.readouterr()
    restarted_state = state_store.load()

    assert exit_code == 0
    assert "Prepared fresh review tracking for 2 changes" in captured.out
    assert f"jj-stack submit {head_change_id}" in captured.out
    for change_id in change_ids:
        assert change_id not in restarted_state.review_identities
        assert change_id not in restarted_state.submitted_baselines

    assert run_main(repo, config_path, "submit", head_change_id) == 0
    capsys.readouterr()
    resubmitted_state = state_store.load()
    new_pr_numbers = {
        change_id: resubmitted_state.review_identities[change_id].pr_number
        for change_id in change_ids
    }

    assert all(pr_number is not None for pr_number in new_pr_numbers.values())
    assert set(new_pr_numbers.values()).isdisjoint(old_pr_numbers.values())
    for change_id, new_pr_number in new_pr_numbers.items():
        assert new_pr_number is not None
        pull_request = fake_repo.pull_requests[new_pr_number]
        assert pull_request.state == "open"
        new_bookmark = resubmitted_state.review_identities[change_id].head_ref
        assert new_bookmark != old_bookmarks[change_id]
        assert new_bookmark.endswith(f"-{short_change_id(change_id)}")
        assert pull_request.head_ref == new_bookmark


def test_restart_dry_run_leaves_tracking_data_unchanged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()

    exit_code = run_main(repo, config_path, "restart", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would prepare fresh review tracking for 1 change" in captured.out
    assert state_store.load() == initial_state


def test_restart_rejects_selected_malformed_identity_without_local_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_path = resolve_state_path(repo)
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["review_identities"][change_id] = {"version": 9}
    write_file(state_path, json.dumps(raw_state))
    bookmarks_before = JjClient(repo).list_bookmark_states()

    exit_code = run_main(repo, config_path, "restart", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "relink" in captured.err
    assert JjClient(repo).list_bookmark_states() == bookmarks_before
    assert json.loads(state_path.read_text(encoding="utf-8")) == raw_state


def test_submit_restart_creates_fresh_pr_on_regenerated_branch_after_head_branch_rename(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    original_identity = state.review_identities[change_id]
    generated_bookmark = original_identity.head_ref
    stale_bookmark = f"review/stale-{short_change_id(change_id)}"
    baseline = state.submitted_baselines[change_id]
    state_store.relink_review(
        change_id,
        expected_identity=original_identity,
        expected_baseline=baseline,
        identity=original_identity.model_copy(update={"head_ref": stale_bookmark}),
        baseline=baseline,
    )

    exit_code = run_main(repo, config_path, "submit", "--restart", change_id)
    capsys.readouterr()
    restarted_identity = state_store.load().review_identities[change_id]

    assert exit_code == 0
    assert restarted_identity.pr_number == 2
    assert restarted_identity.head_ref not in {stale_bookmark, generated_bookmark}
    assert restarted_identity.head_ref.endswith(f"-{short_change_id(change_id)}")
    assert fake_repo.pull_requests[1].head_ref == generated_bookmark
    assert fake_repo.pull_requests[2].head_ref == restarted_identity.head_ref


def test_submit_restart_preserves_old_tracking_when_fresh_branch_has_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    short_id = short_change_id(change_id)
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    original_identity = state.review_identities[change_id]
    fresh_bookmark = (
        original_identity.head_ref.removesuffix(f"-{short_id}") + f"-fresh-pr1-{short_id}"
    )
    stale_bookmark = f"review/stale-{short_id}"
    stale_identity = original_identity.model_copy(update={"head_ref": stale_bookmark})
    baseline = state.submitted_baselines[change_id]
    stale_state = state_store.relink_review(
        change_id,
        expected_identity=original_identity,
        expected_baseline=baseline,
        identity=stale_identity,
        baseline=baseline,
    )
    fake_repo.create_pull_request(
        base_ref="main",
        body="collision",
        head_ref=fresh_bookmark,
        title="collision",
    )

    exit_code = run_main(repo, config_path, "submit", "--restart", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub already reports PR #2" in captured.err
    assert state_store.load() == stale_state


def test_restart_rejects_unlinked_change_without_rewriting_tracking_state(
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
    second_change_id = stack.revisions[1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    state_store.set_link_state(
        second_change_id,
        expected_identity=submitted_state.review_identities[second_change_id],
        link_state="unlinked",
    )
    unlinked_state = state_store.load()

    exit_code = run_main(repo, config_path, "restart", stack.head.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unlinked from review tracking" in captured.err
    assert state_store.load() == unlinked_state
