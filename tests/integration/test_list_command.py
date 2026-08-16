from __future__ import annotations

import json

from jj_stack.errors import EXIT_INCOMPLETE
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore

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
    approve_pull_requests,
    configure_submit_environment,
    patch_github_client_builders,
    run_main,
)


def test_list_json_reports_public_stack_rows(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "list")
    assert set(payload) == {"rows"}

    row = payload["rows"][0]
    assert row["type"] == "stack"
    assert row["status"] == "open"
    assert row["subject"] == "feature 1"
    assert len(row["changes"]) == 1

    change = row["changes"][0]
    assert change["change_id"] == change_id
    assert change["branch"].startswith("jj-stack/feature-1-")
    assert change["pull_request"]["number"] == 1
    assert change["status"] == "open"
    assert "head_change_id" not in row
    assert "pull_requests" not in row
    assert "review" not in row
    assert "size" not in row


def test_list_surfaces_orphaned_pull_request_after_change_is_abandoned(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    orphaned_change_id = stack.revisions[0].change_id
    state = ReviewStateStore.for_repo(repo).load()
    orphaned_pr_number = state.review_identities[orphaned_change_id].pr_number
    orphaned_branch = state.review_identities[orphaned_change_id].head_ref

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
    assert orphan["pull_request"]["number"] == orphaned_pr_number
    assert "hint" not in orphan


def test_list_surfaces_orphaned_pull_request_when_no_live_stacks_remain(
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
    assert f"jj-stack submit {change_id[:8]}" in normalized_err


def test_list_treats_a_visible_submitted_predecessor_as_published(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    branch = ReviewStateStore.for_repo(repo).load().review_identities[change_id].head_ref
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", branch], repo)

    assert run_main(repo, config_path, "list", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["rows"]) == 1
    assert [change["change_id"] for change in payload["rows"][0]["changes"]] == [change_id]


def test_list_extends_tracked_stack_through_unsubmitted_local_descendant(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 2", "feature-2.txt")
    head_change_id = selected_stack(repo).head.change_id

    exit_code = run_main(repo, config_path, "ls")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert head_change_id[:8] in captured.out
    assert "feature 2" in captured.out
    assert "2 changes" in captured.out
    assert "PR" in captured.out
    assert "1" in captured.out

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "list")
    changes = payload["rows"][0]["changes"]
    unsubmitted = next(change for change in changes if change["change_id"] == head_change_id)
    assert unsubmitted["status"] == "unsubmitted"
    assert "branch" not in unsubmitted


def test_list_keeps_one_stack_when_saved_tracking_is_sparse_in_the_middle(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 1", "feature-1.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = selected_stack(repo)
    middle_change_id = stack.revisions[1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_store.retire_review(middle_change_id)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.count("feature 3") == 1
    assert "3 changes" in captured.out
    assert "1 change" not in captured.out


def test_list_inventories_paths_that_share_a_reviewed_prefix(
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
    right = JjClient(repo).resolve_revision("@")
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

    approve_pull_requests(fake_repo, 1)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "1 approved" in captured.out
    assert "1 approved, open" in captured.out


def test_list_reports_no_stacks_when_state_is_empty(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No stacks." in captured.out


def test_list_does_not_extend_through_modified_working_copy(
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


def test_list_does_not_extend_through_another_workspaces_working_copy(
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

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert len(payload["rows"]) == 1
    assert [change["change_id"] for change in payload["rows"][0]["changes"]] == [
        feature_change_id
    ]


def test_list_falls_back_when_github_unavailable(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.list_", "jj_stack.review.status"),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == EXIT_INCOMPLETE
    assert "GitHub unavailable" in captured.out
    assert "feature 1" in captured.out


def test_list_marks_stale_saved_pull_request_link_and_exits_nonzero(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    fake_repo.pull_requests.clear()

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == EXIT_INCOMPLETE
    assert "stale link" in captured.out
    assert "PR 1" in captured.out


def test_list_and_view_agree_that_a_divergent_change_is_an_incomplete_report(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """One repository must not look complete to `list` and incomplete to `view`.

    `list` already labels the row `divergent`, so exiting 0 told a script the report could be
    trusted while `view` reported the same repository as incomplete.
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
    assert list_exit_code == EXIT_INCOMPLETE
    assert view_exit_code == EXIT_INCOMPLETE
    assert "divergent" in view_output.err
