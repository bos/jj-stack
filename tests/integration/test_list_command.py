from __future__ import annotations

import json

from jj_stack.errors import EXIT_INCOMPLETE
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    OfflineGithubClient,
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    patch_github_client_builders,
    run_command,
    selected_stack,
    write_file,
)
from ..support.json_schema import assert_json_output_matches_schema
from ..support.output_assertions import assert_output_contains
from .submit_command_helpers import (
    configure_submit_environment,
    run_main,
)


def test_list_reports_public_stack_rows_and_links_live_pr(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    fake_repo.prs[1].check_rollup_state = "PENDING"

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "list")

    row = payload["rows"][0]
    assert row["type"] == "stack"
    assert row["status"] == "open, checks pending"
    assert row["subject"] == "feature 1"
    assert len(row["changes"]) == 1

    change = row["changes"][0]
    assert change["change_id"] == change_id
    assert change["branch"].startswith("jj-stack/feature-1-")
    assert change["pr"]["number"] == 1
    assert change["pr"]["checks"] == "pending"
    assert change["status"] == "open"

    run_command(["jj", "describe", "-r", change_id, "-m", "feature \x1bc"], repo)
    assert run_main(repo, config_path, "list", "--color=always") == 0
    terminal_output = capsys.readouterr().out
    assert "\x1bc" not in terminal_output
    assert "feature c" in terminal_output
    assert "PR 1" in terminal_output
    assert "https://github.test/octo-org/stacked-prs/pull/1" in terminal_output


def test_list_surfaces_orphaned_pr_after_change_is_abandoned(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    orphaned_change_id = stack.changes[0].change_id
    state = TrackingStore.for_repo(repo).load()
    orphaned_pr_number = state.prs[orphaned_change_id].pr_identity.pr_number
    orphaned_branch = state.prs[orphaned_change_id].pr_identity.head_ref

    run_command(["jj", "abandon", orphaned_change_id], repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "orphan" in captured.out
    assert f"PR #{orphaned_pr_number}" in captured.out
    assert "local change missing" in captured.out
    assert_output_contains(captured.out, "cleanup --pull-request orphans --close")

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "list")

    orphan_rows = [row for row in payload["rows"] if row["type"] == "orphan"]
    assert len(orphan_rows) == 1
    orphan = orphan_rows[0]
    assert orphan["change_id"] == orphaned_change_id
    assert orphan["branch"] == orphaned_branch
    assert orphan["subject"] == "local change missing"
    assert orphan["status"] == "orphan"
    assert orphan["pr"]["number"] == orphaned_pr_number
    assert "hint" not in orphan


def test_list_surfaces_orphaned_pr_when_no_live_stacks_remain(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).head.change_id
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert change_id[:8] in captured.out
    assert "PR #1" in captured.out
    assert "orphan" in captured.out
    assert "No stacks." not in captured.out

    json_exit_code = run_main(repo, config_path, "list", "--json")
    payload = json.loads(capsys.readouterr().out)

    assert json_exit_code == 0
    assert_json_output_matches_schema(payload, "list")
    assert [row["type"] for row in payload["rows"]] == ["orphan"]


def test_list_warns_when_tracked_stack_was_rewritten_without_moving(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).head.change_id
    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert change_id[:8] in captured.err
    assert "changed since its last submit" in captured.err
    assert f"jj-stack view {change_id[:8]}" in normalized_err


def test_list_treats_a_visible_submitted_predecessor_as_published(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    branch = TrackingStore.for_repo(repo).load().prs[change_id].pr_identity.head_ref
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", branch], repo)
    fake_repo.create_pr_review(pr_number=1, reviewer_login="alice", state="APPROVED")

    assert run_main(repo, config_path, "list", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["rows"]) == 1
    assert [change["change_id"] for change in payload["rows"][0]["changes"]] == [change_id]
    change = payload["rows"][0]["changes"][0]
    assert change["status"] == "approved"
    assert change["needs_submit"] is True


def test_list_links_top_pr_below_unsubmitted_local_descendant(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    # PR numbers no longer follow stack order after a local reorder.
    bottom, top = selected_stack(repo).changes
    run_command(["jj", "rebase", "-r", bottom.change_id, "-A", top.change_id], repo)
    commit_file(repo, "feature 3", "feature-3.txt")
    head_change_id = selected_stack(repo).head.change_id

    exit_code = run_main(repo, config_path, "ls", "--color=always")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "feature 3" in captured.out
    assert "3 changes" in captured.out
    assert "2 PRs" in captured.out
    assert "https://github.test/octo-org/stacked-prs/pull/1" in captured.out
    assert "https://github.test/octo-org/stacked-prs/pull/2" not in captured.out

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "list")
    assert payload["rows"][0]["head_change_id"] == head_change_id
    changes = payload["rows"][0]["changes"]
    unsubmitted = next(change for change in changes if change["change_id"] == head_change_id)
    assert unsubmitted["status"] == "unsubmitted"
    assert unsubmitted["needs_submit"] is False
    assert "branch" not in unsubmitted


def test_list_keeps_one_stack_when_saved_tracking_is_sparse_in_the_middle(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    bottom, middle, top = stack.changes
    # Forget the lower prefix, then reattach only its bottom PR. Tracking is sparse while
    # the jj parent chain still connects all three changes.
    assert run_main(repo, config_path, "unstack", "--local", middle.change_id) == 0
    assert run_main(repo, config_path, "relink", "1", bottom.change_id) == 0
    assert set(TrackingStore.for_repo(repo).load().prs) == {bottom.change_id, top.change_id}
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.count("feature 3") == 1
    assert "3 changes" in captured.out
    assert "1 change" not in captured.out


def test_list_inventories_paths_that_share_a_submitted_prefix(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "shared", "shared.txt")
    shared = selected_stack(repo).head
    commit_file(repo, "left", "left.txt")
    left = selected_stack(repo).head
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "new", shared.commit_id, "-m", "right"], repo)
    write_file(repo / "right.txt", "right\n")
    right = JjClient(repo).resolve_commit("@")
    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--base",
            shared.change_id,
            right.change_id,
        )
        == 0
    )
    capsys.readouterr()

    assert run_main(repo, config_path, "list", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    paths = {
        tuple(change["change_id"] for change in row["changes"])
        for row in payload["rows"]
        if row["type"] == "stack"
    }
    current_paths = {
        tuple(change["change_id"] for change in row["changes"])
        for row in payload["rows"]
        if row["type"] == "stack" and row.get("current")
    }

    assert paths == {
        (shared.change_id, left.change_id),
        (shared.change_id, right.change_id),
    }
    assert current_paths == {(shared.change_id, right.change_id)}


def test_list_reports_partial_approval_for_ready_prefix_only(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    fake_repo.create_pr_review(
        pr_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )
    fake_repo.prs[1].check_rollup_state = "SUCCESS"
    fake_repo.prs[2].check_rollup_state = "FAILURE"

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "1 approved" in captured.out
    assert "1 approved, open" in captured.out
    assert "1 approved, open, checks failed" in captured.out


def test_list_omits_wholly_untracked_local_stacks(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "unsubmitted feature", "feature.txt")

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No stacks." in captured.out
    assert "unsubmitted feature" not in captured.out


def test_list_does_not_extend_through_undescribed_working_copy(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    feature_change_id = selected_stack(repo).head.change_id
    write_file(repo / "scratch.txt", "in progress\n")

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"@ {feature_change_id[:8]}" in captured.out
    assert "feature 1" in captured.out
    assert "1 change" in captured.out


def test_list_extends_through_another_workspaces_described_working_copy(
    tmp_path,
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
    write_file(other_workspace / "other.txt", "other\n")
    run_command(["jj", "describe", "-m", "other work"], other_workspace)
    other_change_id = JjClient(other_workspace).resolve_commit("@").change_id

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert len(payload["rows"]) == 1
    assert [change["change_id"] for change in payload["rows"][0]["changes"]] == [
        feature_change_id,
        other_change_id,
    ]


def test_list_falls_back_when_github_unavailable(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    app = create_app(FakeGithubState.single_repo(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == EXIT_INCOMPLETE
    assert "GitHub unavailable" in captured.out
    assert "GitHub unavailable" in captured.err
    assert "feature 1" in captured.out


def test_list_and_view_agree_that_a_divergent_change_is_an_incomplete_report(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """One repo must not look complete to `list` and incomplete to `view`.

    `list` already labels the row `divergent`, so exiting 0 told a script the report could be
    trusted while `view` reported the same repo as incomplete.
    """

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id

    # Two concurrent operations rewriting one change is how divergence reaches a tracked
    # stack in real use, such as edits made from two workspaces or two machines.
    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 here"], repo)
    run_command(
        ["jj", "describe", "--at-operation", "@-", "-r", change_id, "-m", "feature 1 elsewhere"],
        repo,
    )

    list_exit_code = run_main(repo, config_path, "list")
    list_output = capsys.readouterr().out
    view_exit_code = run_main(repo, config_path, "view")
    view_output = capsys.readouterr()

    assert "divergent" in list_output
    assert "jj converge -r" in list_output
    assert list_exit_code == EXIT_INCOMPLETE
    assert view_exit_code == EXIT_INCOMPLETE
    assert "jj converge -r" in view_output.out
    assert "divergent" in view_output.out
