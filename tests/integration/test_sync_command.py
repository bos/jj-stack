from __future__ import annotations

from pathlib import Path

from jj_stack.jj.client import JjClient

from ..support.integration_helpers import (
    init_fake_github_repo_with_submitted_stack,
)
from .submit_command_helpers import (
    approve_pull_requests,
    configure_submit_environment,
    read_remote_ref,
    run_main,
)


def _merge_pull_request(fake_repo, pull_number: int) -> None:
    fake_repo.pull_requests[pull_number].state = "closed"
    fake_repo.pull_requests[pull_number].merged_at = "2026-03-16T12:00:00Z"


def test_sync_rebases_off_merged_ancestor_and_resubmits(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[1].change_id
    trunk_commit_id = stack.trunk.commit_id
    _merge_pull_request(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", top_change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied rebase actions:" in captured.out
    assert "Submitted changes:" in captured.out
    rewritten_top = JjClient(repo).resolve_revision(top_change_id)
    assert rewritten_top.only_parent_commit_id() == trunk_commit_id
    # The surviving PR now targets trunk instead of the merged review branch.
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[2].state == "open"


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
    assert "Planned rebase actions:" in captured.out
    assert "Submit preview skipped" in captured.out
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
    assert "Nothing to submit: everything on the selected stack has merged." in captured.out
    # No replacement pull request was opened for the merged change.
    assert set(fake_repo.pull_requests) == {1}


def test_sync_without_merged_changes_resubmits_the_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No merged changes on the selected stack need rebasing." in captured.out
    assert "Submitted changes:" in captured.out
    assert fake_repo.pull_requests[1].state == "open"


def test_sync_completes_the_protected_trunk_flow_after_land_via_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """land --via merge then sync is the full protected-trunk workflow."""

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

    sync_exit_code = run_main(repo, config_path, "sync", top_change_id)
    captured = capsys.readouterr()

    assert sync_exit_code == 0
    assert "Applied rebase actions:" in captured.out
    assert "Submitted changes:" in captured.out
    # The surviving change now sits on the squash-merged trunk tip and its PR
    # targets trunk.
    merged_trunk_commit = read_remote_ref(fake_repo.git_dir, "main")
    rewritten_top = JjClient(repo).resolve_revision(top_change_id)
    assert rewritten_top.only_parent_commit_id() == merged_trunk_commit
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[2].state == "open"
