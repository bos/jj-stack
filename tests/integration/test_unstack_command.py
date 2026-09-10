from __future__ import annotations

from pathlib import Path

from jj_stack.github.client import GithubClient
from jj_stack.state.store import TrackingStore

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    patch_github_client_builders,
    selected_stack,
)
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    run_main,
)


def _combined_output(captured) -> str:
    return " ".join((captured.out + " " + captured.err).split())


def test_unstack_removes_stack_without_closing_prs_or_forgetting_links(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = TrackingStore.for_repo(repo)
    state_before = state_store.load()
    fake_repo.github_stacks = {7: (1, 2)}

    preview_exit_code = run_main(repo, config_path, "unstack", "--dry-run", change_id)
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "Would remove GitHub stack #7" in preview.out
    assert fake_repo.github_stacks == {7: (1, 2)}

    exit_code = run_main(repo, config_path, "unstack", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Removed GitHub stack #7" in captured.out
    assert fake_repo.github_stacks == {}

    assert run_main(repo, config_path, "unstack", change_id) == 0
    assert "No GitHub stack was found" in capsys.readouterr().out
    assert all(pr.state == "open" for pr in fake_repo.prs.values())
    assert state_store.load() == state_before


def test_unstack_by_number_does_not_require_local_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    change_id = selected_stack(repo).head.change_id
    fake_repo.github_stacks = {7: (1, 2)}

    assert run_main(repo, config_path, "unstack", "--local", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "unstack", "--stack", "7")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Removed GitHub stack #7" in captured.out
    assert fake_repo.github_stacks == {}
    assert all(pr.state == "open" for pr in fake_repo.prs.values())
    assert state_store.load().prs == {}

    retry_exit_code = run_main(repo, config_path, "unstack", "--stack", "7")
    retry = capsys.readouterr()

    assert retry_exit_code == 0
    assert "No GitHub stack #7 was found" in retry.out


def test_unstack_by_number_reports_a_stack_github_keeps_because_it_is_merged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    for pr in fake_repo.prs.values():
        pr.state = "closed"
        pr.merged_at = "2026-08-13T12:00:00Z"
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "unstack", "--stack", "7")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "only merged PRs" in " ".join(captured.out.split())
    assert "Removed GitHub stack" not in captured.out
    assert fake_repo.github_stacks == {7: (1, 2)}


def test_unstack_locked_stack_stops_without_closing_or_forgetting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    state_before = state_store.load()
    fake_repo.github_stacks = {7: (1, 2)}
    app = create_app(FakeGithubState.single_repo(fake_repo))

    class LockedStackClient(GithubClient):
        async def unstack(self, *, stack_number):
            fake_repo.prs[1].is_queued = True
            return await super().unstack(stack_number=stack_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=LockedStackClient,
    )

    exit_code = run_main(repo, config_path, "unstack", "--stack", "7")
    captured = capsys.readouterr()
    retry_exit_code = run_main(repo, config_path, "unstack", "--stack", "7")
    retry = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub stack #7 still contains #1" in _combined_output(captured)
    assert retry_exit_code == 1, retry
    assert "could not remove any pull requests" in _combined_output(retry).lower()
    assert fake_repo.github_stacks == {7: (1,)}
    assert all(pr.state == "open" for pr in fake_repo.prs.values())
    assert state_store.load() == state_before


def test_unstack_rechecks_saved_pr_before_removing_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    change_id = selected_stack(repo).head.change_id
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.prs[1].head_ref = "jj-stack/moved-aaaaaaaa"
    fake_repo.prs[1].head_label = "octo-org:jj-stack/moved-aaaaaaaa"

    exit_code = run_main(repo, config_path, "unstack", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "now uses head branch" in _combined_output(captured)
    assert fake_repo.github_stacks == {7: (1, 2)}
    assert state_store.load() == initial_state


def test_unstack_local_forgets_links_without_changing_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = TrackingStore.for_repo(repo)
    branch = state_store.load().prs[change_id].pr_identity.head_ref

    preview_exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--local",
        "--dry-run",
        change_id,
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "Would forget saved pull request links" in preview.out
    assert change_id in state_store.load().prs

    exit_code = run_main(repo, config_path, "unstack", "--local", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Forgot saved pull request links" in captured.out
    assert fake_repo.prs[1].state == "open"
    assert change_id not in state_store.load().prs
    assert read_remote_ref(fake_repo.git_dir, branch)
