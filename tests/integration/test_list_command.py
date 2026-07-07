from __future__ import annotations

import json
from typing import ClassVar

from jj_stack.errors import EXIT_INCOMPLETE
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
    write_file,
)
from ..support.json_schema import assert_json_output_matches_schema
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
    change_id = JjClient(repo).discover_review_stack(allow_immutable=True).head.change_id

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
    assert change["bookmark"].startswith("review/feature-1-")
    assert change["pull_request"]["number"] == 1
    assert change["status"] == "open"
    assert "head_change_id" not in row
    assert "pull_requests" not in row
    assert "review" not in row
    assert "size" not in row


def test_list_reports_multiple_locally_tracked_stacks(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 1", "feature-1.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "feature 1" in captured.out
    assert "feature 2" in captured.out
    assert "PR" in captured.out
    assert "1" in captured.out
    assert "2" in captured.out
    assert "1 change" in captured.out


def test_list_summarizes_multi_pull_request_stack_by_count(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    for index in range(5):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "5 PRs" in captured.out
    assert "PRs 1-5" not in captured.out
    assert "PRs 1, 2, 3, 4, 5" not in captured.out
    assert "feature 5" in captured.out

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert [
        change["pull_request"]["number"] for change in payload["rows"][0]["changes"]
    ] == [1, 2, 3, 4, 5]


def test_list_surfaces_orphaned_pull_request_after_change_is_abandoned(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "alpha 1", "alpha-1.txt")
    commit_file(repo, "alpha 2", "alpha-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    middle_change_id = stack.revisions[0].change_id
    state = ReviewStateStore.for_repo(repo).load()
    middle_pr_number = state.changes[middle_change_id].pr_number
    assert middle_pr_number is not None

    run_command(["jj", "abandon", middle_change_id], repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "orphan" in captured.out
    assert f"PR #{middle_pr_number}" in captured.out
    assert "local change missing" in captured.out
    assert f"unstack --cleanup --pull-request {middle_pr_number}" in captured.out


def test_list_json_reports_public_orphan_rows(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "alpha 1", "alpha-1.txt")
    commit_file(repo, "alpha 2", "alpha-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    middle_change_id = stack.revisions[0].change_id
    state = ReviewStateStore.for_repo(repo).load()
    middle_pr_number = state.changes[middle_change_id].pr_number
    assert middle_pr_number is not None

    run_command(["jj", "abandon", middle_change_id], repo)

    exit_code = run_main(repo, config_path, "list", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert_json_output_matches_schema(payload, "list")

    orphan_rows = [row for row in payload["rows"] if row["type"] == "orphan"]
    assert len(orphan_rows) == 1
    orphan = orphan_rows[0]
    assert orphan["change_id"] == middle_change_id
    assert orphan["subject"] == "local change missing"
    assert orphan["status"] == "orphan"
    assert orphan["pull_request"]["number"] == middle_pr_number
    assert "hint" not in orphan


def test_list_surfaces_orphaned_pull_request_when_no_live_stacks_remain(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert change_id[:8] in captured.out
    assert "PR #1" in captured.out
    assert "orphan" in captured.out
    assert "No stacks." not in captured.out


def test_list_warns_when_tracked_stack_has_changed_since_last_submit(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "alpha 1", "alpha-1.txt")
    commit_file(repo, "alpha 2", "alpha-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    commit_file(repo, "alpha 3", "alpha-3.txt")
    new_alpha_head_change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert new_alpha_head_change_id[:8] in captured.err
    assert "changed since its last submit" in captured.err
    assert f"jj-stack view {new_alpha_head_change_id[:8]}" in normalized_err
    assert f"jj-stack submit {new_alpha_head_change_id[:8]}" in normalized_err


def test_list_warns_when_tracked_stack_was_rewritten_without_moving(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert change_id[:8] in captured.err
    assert "changed since its last submit" in captured.err
    assert f"jj-stack view {change_id[:8]}" in normalized_err
    assert f"jj-stack submit {change_id[:8]}" in normalized_err


def test_list_warns_when_untracked_change_is_inserted_below_tracked_stack(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "alpha 1", "alpha-1.txt")
    commit_file(repo, "alpha 2", "alpha-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    head_change_id = stack.head.change_id

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "alpha 0", "alpha-0.txt")
    inserted_change_id = JjClient(repo).discover_review_stack().head.change_id
    run_command(["jj", "rebase", "-s", bottom_change_id, "-d", inserted_change_id], repo)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert head_change_id[:8] in captured.err
    assert f"jj-stack view {head_change_id[:8]}" in normalized_err
    assert f"jj-stack submit {head_change_id[:8]}" in normalized_err


def test_list_does_not_warn_when_tracked_stack_still_starts_at_mutable_trunk(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    run_command(
        ["jj", "config", "set", "--repo", 'revset-aliases."immutable_heads()"', "none()"],
        repo,
    )

    commit_file(repo, "alpha 1", "alpha-1.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    head_change_id = JjClient(repo).discover_review_stack().head.change_id

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert head_change_id[:8] not in captured.err


def test_list_extends_tracked_stack_through_unsubmitted_local_descendant(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 2", "feature-2.txt")
    head_change_id = JjClient(repo).discover_review_stack().head.change_id

    exit_code = run_main(repo, config_path, "ls")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert head_change_id[:8] in captured.out
    assert "feature 2" in captured.out
    assert "2 changes" in captured.out
    assert "PR" in captured.out
    assert "1" in captured.out


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

    stack = JjClient(repo).discover_review_stack(allow_immutable=True)
    middle_change_id = stack.revisions[1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    changes = dict(state.changes)
    del changes[middle_change_id]
    state_store.save(state.model_copy(update={"changes": changes}))

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.count("feature 3") == 1
    assert "3 changes" in captured.out
    assert "1 change" not in captured.out


def test_list_keeps_current_tracked_stack_when_it_becomes_immutable(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    feature = JjClient(repo).discover_review_stack(allow_immutable=True).head.commit_id
    run_command(
        [
            "jj",
            "config",
            "set",
            "--repo",
            'revset-aliases."immutable_heads()"',
            f"builtin_immutable_heads() | {feature}",
        ],
        repo,
    )
    head_change_id = JjClient(repo).discover_review_stack(allow_immutable=True).head.change_id

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No stacks." not in captured.out
    assert f"@ {head_change_id[:8]}" in captured.out
    assert "feature 1" in captured.out
    assert "PR 1" in captured.out
    assert "1 change" in captured.out


def test_list_reports_partial_approval_for_ready_prefix_only(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    for index in range(4):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    approve_pull_requests(fake_repo, 1, 2)

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "2 approved" in captured.out
    assert "2 open" in captured.out


def test_list_reports_cleanup_needed_for_merged_pull_request(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "cleanup needed" in captured.out
    assert "merged ancestor" not in captured.out


def test_list_batches_github_lookup_across_repo_stacks(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 1", "feature-1.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    class CountingGithubClient(GithubClient):
        pull_request_lookup_calls: ClassVar[list[tuple[str, ...]]] = []
        review_decision_calls: ClassVar[list[tuple[int, ...]]] = []

        async def get_pull_requests_by_head_refs(self, *, head_refs):
            self.pull_request_lookup_calls.append(tuple(sorted(head_refs)))
            return await super().get_pull_requests_by_head_refs(head_refs=head_refs)

        async def get_review_decisions_by_pull_request_numbers(
            self,
            *,
            pull_numbers,
        ):
            self.review_decision_calls.append(tuple(sorted(pull_numbers)))
            return await super().get_review_decisions_by_pull_request_numbers(
                pull_numbers=pull_numbers,
            )

    app = create_app(FakeGithubState.single_repository(fake_repo))
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.list_", "jj_stack.review.status"),
        client_type=CountingGithubClient,
    )

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "feature 1" in captured.out
    assert "feature 2" in captured.out
    assert len(CountingGithubClient.pull_request_lookup_calls) == 1
    assert len(CountingGithubClient.pull_request_lookup_calls[0]) == 2
    assert CountingGithubClient.review_decision_calls == []


def test_list_fails_closed_when_tracked_changes_share_bookmark(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 1", "feature-1.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    change_ids = tuple(state.changes)
    shared_bookmark = state.changes[change_ids[0]].bookmark
    assert shared_bookmark is not None
    changes = dict(state.changes)
    changes[change_ids[1]] = changes[change_ids[1]].model_copy(
        update={"bookmark": shared_bookmark}
    )
    state_store.save(state.model_copy(update={"changes": changes}))

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not safely inspect stacks" in captured.err
    assert "same bookmark" in captured.err


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

    feature_change_id = JjClient(repo).discover_review_stack().head.change_id
    write_file(repo / "scratch.txt", "in progress\n")

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"@ {feature_change_id[:8]}" in captured.out
    assert "feature 1" in captured.out
    assert "1 change" in captured.out


def test_list_limits_bookmark_scan_to_tracked_bookmarks(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 2", "feature-2.txt")
    tracked_change_id = JjClient(repo).discover_review_stack().revisions[0].change_id
    tracked_bookmark = ReviewStateStore.for_repo(repo).load().changes[tracked_change_id].bookmark
    assert tracked_bookmark is not None

    bookmark_calls: list[tuple[str, ...] | None] = []
    original_list_bookmark_states = JjClient.list_bookmark_states

    def tracking_list_bookmark_states(self, bookmarks=None):
        bookmark_calls.append(None if bookmarks is None else tuple(bookmarks))
        return original_list_bookmark_states(self, bookmarks)

    monkeypatch.setattr(
        JjClient,
        "list_bookmark_states",
        tracking_list_bookmark_states,
    )

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "feature 2" in captured.out
    assert bookmark_calls == [(tracked_bookmark,)]


def test_list_marks_unlinked_change(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "list")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "unlinked" in captured.out


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
