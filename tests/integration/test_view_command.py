from __future__ import annotations

import json
from pathlib import Path

from jj_stack.errors import EXIT_AMBIGUOUS, EXIT_FAILURE, EXIT_INCOMPLETE, EXIT_NO_STACK
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore, resolve_state_path

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
    write_file,
)
from ..support.json_schema import assert_json_output_matches_schema
from ..support.output_assertions import assert_output_contains
from .submit_command_helpers import (
    configure_submit_environment,
    patch_github_client_builders,
    run_main,
)


def test_view_json_reports_public_stack_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id

    exit_code = run_main(repo, config_path, "view", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "view")
    assert set(payload) == {"stacks"}

    stack = payload["stacks"][0]
    assert set(stack) == {"changes"}

    change = stack["changes"][0]
    assert {
        "branch",
        "change_id",
        "pr",
        "status",
        "subject",
    } <= set(change)
    assert set(change) <= {
        "branch",
        "change_id",
        "current",
        "pr",
        "status",
        "subject",
    }
    assert change["change_id"] == change_id
    assert change["branch"].startswith("jj-stack/feature-1-")
    assert change["status"] == "open"
    assert change["subject"] == "feature 1"
    assert change["pr"]["number"] == 1
    assert "remote_branch" not in change
    assert "saved_pr" not in change


def test_view_and_list_show_queued_prs(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.prs[1].is_queued = True

    assert run_main(repo, config_path, "view") == 0
    viewed = capsys.readouterr()
    assert "PR #1 queued" in viewed.out

    assert run_main(repo, config_path, "list") == 0
    listed = capsys.readouterr()
    assert "queued" in listed.out


def test_view_warns_and_reports_empty_working_copy_from_another_workspace(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    feature_change_id = selected_stack(repo).head.change_id
    other_workspace = tmp_path / "other-workspace"
    run_command(
        [
            "jj",
            "workspace",
            "add",
            "--name",
            "other",
            "--revision",
            feature_change_id,
            str(other_workspace),
        ],
        repo,
    )
    other_working_copy = JjClient(other_workspace).resolve_commit("@")

    exit_code = run_main(repo, config_path, "view", other_working_copy.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert other_working_copy.change_id[:8] in captured.out
    assert other_working_copy.change_id[:8] in captured.err
    assert "empty working-copy change" in captured.err


def test_view_warns_and_reports_merge_commit_first_parent_path(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature left", "left.txt")
    left = selected_stack(repo).head
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature right", "right.txt")
    right = selected_stack(repo).head
    run_command(["jj", "new", "-m", "merge head", left.commit_id, right.commit_id], repo)
    merge = JjClient(repo).resolve_commit("@")

    exit_code = run_main(repo, config_path, "view", "@")
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "merge head" in captured.out
    assert "feature left" in captured.out
    assert merge.change_id[:8] in captured.err
    assert "first-parent" in captured.err


def test_view_warns_and_reports_undescribed_working_copy_inside_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    feature_change_id = selected_stack(repo).head.change_id
    undescribed_workspace = tmp_path / "undescribed-workspace"
    run_command(
        [
            "jj",
            "workspace",
            "add",
            "--name",
            "undescribed",
            "--revision",
            feature_change_id,
            str(undescribed_workspace),
        ],
        repo,
    )
    write_file(undescribed_workspace / "undescribed.txt", "undescribed\n")
    run_command(["jj", "status"], undescribed_workspace)
    undescribed = JjClient(undescribed_workspace).resolve_commit("@")
    child_workspace = tmp_path / "child-workspace"
    run_command(
        [
            "jj",
            "workspace",
            "add",
            "--name",
            "child",
            "--revision",
            undescribed.change_id,
            str(child_workspace),
        ],
        repo,
    )
    commit_file(child_workspace, "feature 2", "feature-2.txt")
    child_change_id = JjClient(child_workspace).resolve_commit("@-").change_id

    exit_code = run_main(repo, config_path, "view", child_change_id)
    captured = capsys.readouterr()
    warning = " ".join(captured.err.split())

    assert exit_code == 0, captured.err
    assert "feature 2" in captured.out
    assert undescribed.change_id[:8] in captured.out
    assert undescribed.change_id[:8] in warning
    assert "no description" in warning


def test_view_warns_and_reports_conflicted_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature conflict", "shared.txt")
    change_id = selected_stack(repo).head.change_id
    run_command(["jj", "new", "main"], repo)
    write_file(repo / "shared.txt", "trunk conflict\n")
    run_command(["jj", "commit", "-m", "trunk conflict"], repo)
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)
    conflicted = JjClient(repo).resolve_commit(change_id)
    assert conflicted.conflict

    exit_code = run_main(repo, config_path, "view", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "feature conflict" in captured.out
    assert conflicted.change_id[:8] in captured.err
    assert "conflict" in captured.err


def test_view_pr_selector_shows_the_complete_containing_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    first_change_id = stack.changes[0].change_id
    second_change_id = stack.changes[1].change_id
    state = TrackingStore.for_repo(repo).load()
    first_pr_number = state.pr_identities[first_change_id].pr_number
    second_pr_number = state.pr_identities[second_change_id].pr_number
    exit_code = run_main(
        repo,
        config_path,
        "view",
        "--pull-request",
        str(first_pr_number),
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Using PR #{first_pr_number} -> {first_change_id}" in captured.out
    assert "feature 1" in captured.out
    assert "PR #1" in captured.out
    assert "feature 2" in captured.out
    assert f"PR #{second_pr_number}" in captured.out


def test_view_change_id_rejects_multiple_containing_paths(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature A", "feature-a.txt")
    commit_file(repo, "feature B", "feature-b.txt")
    commit_file(repo, "feature C", "feature-c.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = selected_stack(repo)
    change_a = stack.changes[0].change_id
    change_b = stack.changes[1].change_id
    change_c = stack.changes[2].change_id

    run_command(["jj", "rebase", "-s", change_c, "-d", change_a], repo)
    run_command(["jj", "edit", change_b], repo)

    exit_code = run_main(repo, config_path, "view", change_a[:8])
    captured = capsys.readouterr()

    assert exit_code == EXIT_AMBIGUOUS
    assert "resolved to more than one commit" in captured.err


def test_view_pr_selector_requires_a_linked_local_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    resolve_state_path(repo).unlink()

    exit_code = run_main(repo, config_path, "view", "--pull-request", "1")
    captured = capsys.readouterr()
    combined_output = " ".join((captured.out + " " + captured.err).split())

    # A single selector that yields no report fails with the error's category
    # code instead of claiming an incomplete report.
    assert exit_code == EXIT_FAILURE
    assert "PR #1 is not linked to any local change." in combined_output


def test_view_reports_missing_trunk_bookmark_in_empty_repo(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    run_command(["jj", "config", "set", "--repo", "user.name", "Test User"], repo)
    run_command(["jj", "config", "set", "--repo", "user.email", "test@example.com"], repo)
    config_path = tmp_path / "jj-stack-config.toml"
    config_path.write_text("[jj-stack]\n", encoding="utf-8")

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    combined = " ".join((captured.out + captured.err).split())

    assert exit_code == EXIT_NO_STACK
    assert "create a trunk bookmark" in combined.lower()


def test_view_reports_missing_git_remote_for_local_only_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path, with_remote=False)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    run_command(["jj", "config", "set", "--repo", 'revset-aliases."trunk()"', "main"], repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    combined_err = " ".join(captured.err.split())

    assert exit_code == EXIT_INCOMPLETE
    assert "no git remote" in combined_err.lower()
    assert "Unsubmitted stack:" in captured.out
    assert "GitHub status unknown" in captured.out


def test_view_renders_base_parent_for_stack_forked_from_trunk_ancestor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    base_commit_id = JjClient(repo).resolve_commit("@-").commit_id

    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)

    run_command(["jj", "new", base_commit_id], repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    stack = selected_stack(repo)

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Unsubmitted stack:" in captured.out
    assert stack.changes[-1].subject in captured.out
    assert stack.base_parent.subject in captured.out
    assert captured.out.index(stack.changes[-1].subject) < captured.out.index(
        stack.base_parent.subject
    )
    assert stack.trunk.subject not in captured.out


def test_view_preserves_remote_observations_when_github_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    app = create_app(FakeGithubState.single_repo(fake_repo))

    class FailingPRLookupClient(GithubClient):
        async def get_open_prs_by_head_refs(self, *, head_refs):
            raise GithubClientError(
                'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
                status_code=404,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.stack.status",),
        client_type=FailingPRLookupClient,
    )

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == EXIT_INCOMPLETE
    assert "GitHub unavailable for octo-org/stacked-prs:" in normalized_err
    assert "repo not found or inaccessible - check GITHUB_TOKEN or gh auth" in normalized_err
    assert "documentation_url" not in captured.out
    assert "saved PR #1" in captured.out


def test_view_stays_local_when_github_is_unavailable_and_no_cache_exists(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repo(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def get_open_prs_by_head_refs(self, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.stack.status",),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert normalized_err == ""
    assert "Unsubmitted stack:" in captured.out
    assert "GitHub status unknown" not in captured.out


def test_view_exits_nonzero_when_github_reports_multiple_prs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    state_store = TrackingStore.for_repo(repo)
    state_before = state_store.load()
    bookmark = state_before.pr_identities[change_id].head_ref
    fake_repo.create_pr(
        base_ref="main",
        body="duplicate",
        head_ref=bookmark,
        title="feature 1 duplicate",
    )

    exit_code = run_main(repo, config_path, "view", change_id)

    assert exit_code == EXIT_INCOMPLETE
    assert state_store.load() == state_before


def test_view_reports_unsubmitted_after_state_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    resolve_state_path(repo).unlink()

    exit_code = run_main(repo, config_path, "view", change_id)
    captured = capsys.readouterr()
    refreshed_state = TrackingStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Unsubmitted stack:" in captured.out
    assert "PR #1" not in captured.out
    assert refreshed_state.pr_identities == {}
    assert refreshed_state.submitted_baselines == {}

    exit_code = run_main(repo, config_path, "view", "--json", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "view")
    change = payload["stacks"][0]["changes"][0]
    assert change["change_id"] == change_id
    assert change["status"] == "unsubmitted"
    assert "branch" not in change


def test_view_preserves_saved_pr_link_when_github_reports_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    assert initial_state.pr_identities[change_id].pr_number == 1

    del fake_repo.prs[1]

    exit_code = run_main(repo, config_path, "view", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == EXIT_INCOMPLETE
    assert "Missing GitHub PR" in captured.out
    assert "remembered PR #1" in captured.out
    assert_output_contains(captured.out, "jj-stack unstack --local")
    assert change_id in captured.out
    assert refreshed_state.pr_identities[change_id].pr_number == 1


def test_view_reports_merged_pr_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    state_store = TrackingStore.for_repo(repo)
    fake_repo.prs[1].state = "closed"
    fake_repo.prs[1].merged_at = "2026-03-16T12:00:00Z"

    exit_code = run_main(repo, config_path, "view", change_id)
    captured = capsys.readouterr()
    state_store.load()

    assert exit_code == 0
    assert "PR #1 merged into main, cleanup needed" in captured.out
