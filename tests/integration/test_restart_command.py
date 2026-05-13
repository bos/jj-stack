from __future__ import annotations

from pathlib import Path

from jj_review.formatting import short_change_id
from jj_review.jj.client import JjClient
from jj_review.models.review_state import CachedChange
from jj_review.state.store import ReviewStateStore

from ..support.integration_helpers import commit_file, init_fake_github_repo
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
    old_records = {change_id: initial_state.changes[change_id] for change_id in change_ids}
    old_bookmarks = {change_id: old_records[change_id].bookmark for change_id in change_ids}
    old_pr_numbers = {change_id: old_records[change_id].pr_number for change_id in change_ids}
    assert all(pr_number is not None for pr_number in old_pr_numbers.values())
    for pr_number in old_pr_numbers.values():
        assert pr_number is not None
        fake_repo.pull_requests[pr_number].state = "closed"

    exit_code = run_main(repo, config_path, "restart", head_change_id)
    captured = capsys.readouterr()
    restarted_state = state_store.load()

    assert exit_code == 0
    assert "Prepared fresh review tracking for 2 changes" in captured.out
    assert f"jj-review submit {head_change_id}" in captured.out
    for change_id in change_ids:
        restarted = restarted_state.changes[change_id]
        assert restarted.bookmark is not None
        assert restarted.bookmark != old_bookmarks[change_id]
        assert restarted.bookmark.endswith(f"-{short_change_id(change_id)}")

    assert run_main(repo, config_path, "submit", head_change_id) == 0
    capsys.readouterr()
    resubmitted_state = state_store.load()
    new_pr_numbers = {
        change_id: resubmitted_state.changes[change_id].pr_number for change_id in change_ids
    }

    assert all(pr_number is not None for pr_number in new_pr_numbers.values())
    assert set(new_pr_numbers.values()).isdisjoint(old_pr_numbers.values())
    for change_id, new_pr_number in new_pr_numbers.items():
        assert new_pr_number is not None
        pull_request = fake_repo.pull_requests[new_pr_number]
        assert pull_request.state == "open"
        assert pull_request.head_ref == resubmitted_state.changes[change_id].bookmark


def test_restart_dry_run_leaves_tracking_data_unchanged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()

    exit_code = run_main(repo, config_path, "restart", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would prepare fresh review tracking for 1 change" in captured.out
    assert state_store.load() == initial_state


def test_submit_restart_creates_new_pull_requests_without_persisting_reset_first(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_change = state_store.load().changes[change_id]
    assert initial_change.pr_number == 1

    exit_code = run_main(repo, config_path, "submit", "--restart", change_id)
    captured = capsys.readouterr()
    restarted_change = state_store.load().changes[change_id]

    assert exit_code == 0
    assert "PR #2" in captured.out
    assert restarted_change.pr_number == 2
    assert restarted_change.bookmark is not None
    assert restarted_change.bookmark != initial_change.bookmark
    assert restarted_change.bookmark.endswith(f"-{short_change_id(change_id)}")
    assert fake_repo.pull_requests[1].head_ref == initial_change.bookmark
    assert fake_repo.pull_requests[2].head_ref == restarted_change.bookmark


def test_submit_restart_does_not_reuse_remembered_pr_after_head_branch_rename(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    original_change = state.changes[change_id]
    generated_bookmark = original_change.bookmark
    assert generated_bookmark is not None
    stale_bookmark = f"review/stale-{short_change_id(change_id)}"
    state_store.save(
        state.model_copy(
            update={
                "changes": {
                    **state.changes,
                    change_id: original_change.model_copy(
                        update={"bookmark": stale_bookmark}
                    ),
                }
            }
        )
    )

    exit_code = run_main(repo, config_path, "submit", "--restart", change_id)
    capsys.readouterr()
    restarted_change = state_store.load().changes[change_id]

    assert exit_code == 0
    assert restarted_change.pr_number == 2
    assert restarted_change.bookmark not in {stale_bookmark, generated_bookmark}
    assert fake_repo.pull_requests[1].head_ref == generated_bookmark
    assert fake_repo.pull_requests[2].head_ref == restarted_change.bookmark


def test_submit_restart_preserves_old_tracking_when_fresh_branch_has_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    change_id = JjClient(repo).discover_review_stack().head.change_id
    short_id = short_change_id(change_id)
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    original_change = state.changes[change_id]
    assert original_change.bookmark is not None
    fresh_bookmark = original_change.bookmark.removesuffix(
        f"-{short_id}"
    ) + f"-fresh-pr1-{short_id}"
    stale_bookmark = f"review/stale-{short_id}"
    stale_change = original_change.model_copy(update={"bookmark": stale_bookmark})
    state_store.save(
        state.model_copy(
            update={"changes": {**state.changes, change_id: stale_change}}
        )
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
    assert state_store.load().changes[change_id] == stale_change


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
    unlinked_state = submitted_state.model_copy(
        update={
            "changes": {
                **submitted_state.changes,
                second_change_id: CachedChange(link_state="unlinked"),
            }
        }
    )
    state_store.save(unlinked_state)

    exit_code = run_main(repo, config_path, "restart", stack.head.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unlinked from review tracking" in captured.err
    assert state_store.load() == unlinked_state
