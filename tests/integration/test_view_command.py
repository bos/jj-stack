from __future__ import annotations

import json
from pathlib import Path

from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
)
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
    change_id = JjClient(repo).discover_review_stack().head.change_id

    exit_code = run_main(repo, config_path, "view", "--json")
    captured = capsys.readouterr()

    assert exit_code == 0
    payload = json.loads(captured.out)
    assert set(payload) == {"stacks"}

    stack = payload["stacks"][0]
    assert set(stack) == {"changes"}

    revision = stack["changes"][0]
    assert {
        "bookmark",
        "change_id",
        "pull_request",
        "status",
        "subject",
    } <= set(revision)
    assert set(revision) <= {
        "bookmark",
        "change_id",
        "current",
        "pull_request",
        "status",
        "subject",
    }
    assert revision["change_id"] == change_id
    assert revision["bookmark"].startswith("review/feature-1-")
    assert revision["status"] == "open"
    assert revision["subject"] == "feature 1"
    assert revision["pull_request"]["number"] == 1
    assert "remote_branch" not in revision
    assert "saved_pull_request" not in revision


def test_view_can_select_a_stack_by_pull_request_number(
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
    first_change_id = stack.revisions[0].change_id
    second_change_id = stack.revisions[1].change_id
    state = ReviewStateStore.for_repo(repo).load()
    first_pr_number = state.changes[first_change_id].pr_number
    second_pr_number = state.changes[second_change_id].pr_number
    assert first_pr_number is not None
    assert second_pr_number is not None

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
    assert "feature 2" not in captured.out
    assert f"PR #{second_pr_number}" not in captured.out


def test_view_does_not_warn_when_unrelated_stack_changed_since_last_submit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "alpha 1", "alpha-1.txt")
    commit_file(repo, "alpha 2", "alpha-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    alpha_head_change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "beta 1", "beta-1.txt")
    commit_file(repo, "beta 2", "beta-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    commit_file(repo, "beta 3", "beta-3.txt")
    new_beta_head_change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id

    run_command(["jj", "edit", alpha_head_change_id], repo)
    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert new_beta_head_change_id[:8] not in captured.err
    assert alpha_head_change_id[:8] not in captured.err
    assert "changed since its last submit" not in captured.err
    assert "jj-stack view" not in normalized_err


def test_view_warns_when_other_stack_is_built_on_selected_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "alpha 1", "alpha-1.txt")
    commit_file(repo, "alpha 2", "alpha-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    alpha_head_change_id = JjClient(repo).discover_review_stack().head.change_id

    commit_file(repo, "beta 1", "beta-1.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    commit_file(repo, "beta 2", "beta-2.txt")
    beta_head_change_id = JjClient(repo).discover_review_stack().head.change_id

    exit_code = run_main(repo, config_path, "view", alpha_head_change_id)
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert beta_head_change_id[:8] in captured.err
    assert "changed since its last submit" in captured.err
    assert f"jj-stack view {beta_head_change_id[:8]}" in normalized_err
    assert f"jj-stack submit {beta_head_change_id[:8]}" in normalized_err


def test_view_warns_after_middle_change_is_split_into_sibling_stack(
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

    stack = JjClient(repo).discover_review_stack()
    change_a = stack.revisions[0].change_id
    change_b = stack.revisions[1].change_id
    change_c = stack.revisions[2].change_id

    run_command(["jj", "rebase", "-s", change_c, "-d", change_a], repo)
    run_command(["jj", "edit", change_b], repo)

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert change_c[:8] in captured.err
    assert "changed since its last submit" in captured.err
    assert f"jj-stack view {change_c[:8]}" in normalized_err
    assert f"jj-stack submit {change_c[:8]}" in normalized_err


def test_view_pull_request_selector_requires_a_linked_local_change(
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

    assert exit_code == 1
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

    assert exit_code == 1
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

    assert exit_code == 1
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
    base_commit_id = JjClient(repo).resolve_revision("@-").commit_id

    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)

    run_command(["jj", "new", base_commit_id], repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    stack = JjClient(repo).discover_review_stack(allow_immutable=True)

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Unsubmitted stack:" in captured.out
    assert stack.revisions[-1].subject in captured.out
    assert stack.base_parent.subject in captured.out
    assert captured.out.index(stack.revisions[-1].subject) < captured.out.index(
        stack.base_parent.subject
    )
    assert stack.trunk.subject not in captured.out


def test_view_ignores_off_path_reviewable_child(
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
    feature_1_commit_id = stack.revisions[0].commit_id
    feature_2_commit_id = stack.revisions[-1].commit_id
    run_command(["jj", "new", feature_1_commit_id], repo)
    commit_file(repo, "feature side", "feature-side.txt")
    run_command(["jj", "new", feature_2_commit_id], repo)

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "feature 2" in captured.out
    assert "feature 1" in captured.out
    assert "feature side" not in captured.out


def test_view_preserves_remote_observations_when_github_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingPullRequestLookupClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError(
                'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
                status_code=404,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.review.status",),
        client_type=FailingPullRequestLookupClient,
    )

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 1
    assert "GitHub unavailable for octo-org/stacked-review:" in normalized_err
    assert "repo not found or inaccessible - check GITHUB_TOKEN or gh auth" in normalized_err
    assert "documentation_url" not in captured.out
    assert "saved PR #1 (open)" in captured.out


def test_view_stays_local_when_github_is_unavailable_and_no_cache_exists(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.review.status",),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert normalized_err == ""
    assert "Unsubmitted stack:" in captured.out
    assert "GitHub status unknown" not in captured.out


def test_view_exits_nonzero_when_pull_request_lookup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingPullRequestLookupClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError(
                'GitHub request failed: 422 {"message":"Validation Failed"}',
                status_code=422,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.review.status",),
        client_type=FailingPullRequestLookupClient,
    )

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "saved PR #1 (open), pull request lookup failed" in captured.out


def test_view_exits_nonzero_when_github_reports_multiple_pull_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    bookmark = state_before.changes[change_id].bookmark
    assert bookmark is not None
    fake_repo.create_pull_request(
        base_ref="main",
        body="duplicate",
        head_ref=bookmark,
        title="feature 1 duplicate",
    )

    exit_code = run_main(repo, config_path, "view", change_id)

    assert exit_code == 1
    assert state_store.load() == state_before


def test_view_skips_stack_comment_github_reads(
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

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailingCommentLookupClient(GithubClient):
        async def list_issue_comments(self, *, issue_number):
            raise AssertionError("status should not inspect stack comments")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.review.status",),
        client_type=FailingCommentLookupClient,
    )

    exit_code = run_main(repo, config_path, "view")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #2" in captured.out
    assert "stack comment" not in captured.out


def test_view_fetch_surfaces_unlinked_state_without_repopulating_link(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "view", "--fetch", change_id)
    captured = capsys.readouterr()
    unlinked_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert "unlinked PR #1" in captured.out
    assert unlinked_change.link_state == "unlinked"
    assert unlinked_change.pr_number is None
    assert unlinked_change.pr_state is None
    assert unlinked_change.pr_url is None
    assert unlinked_change.navigation_comment_id is None
    assert unlinked_change.overview_comment_id is None


def test_view_reports_unsubmitted_after_state_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    resolve_state_path(repo).unlink()

    exit_code = run_main(repo, config_path, "view", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Unsubmitted stack:" in captured.out
    assert "PR #1" not in captured.out
    assert refreshed_state.changes == {}


def test_view_stays_local_after_state_loss_even_if_github_is_unavailable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    resolve_state_path(repo).unlink()

    assert run_main(repo, config_path, "view", change_id) == 0
    capsys.readouterr()

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.review.status",),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "view", change_id)
    captured = capsys.readouterr()
    normalized_err = " ".join(captured.err.split())

    assert exit_code == 0
    assert normalized_err == ""
    assert "Unsubmitted stack:" in captured.out
    assert "saved PR #1" not in captured.out


def test_view_preserves_cached_pull_request_metadata_when_github_reports_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    assert initial_state.changes[change_id].pr_number == 1
    assert initial_state.changes[change_id].navigation_comment_id is None
    assert initial_state.changes[change_id].overview_comment_id is None

    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "view", "--fetch", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "Missing GitHub PR" in captured.out
    assert "remembered PR #1" in captured.out
    assert "jj-stack submit --restart" in captured.out
    assert change_id in captured.out
    assert refreshed_state.changes[change_id].pr_number == 1
    assert refreshed_state.changes[change_id].pr_state == "open"
    assert (
        refreshed_state.changes[change_id].pr_url
        == "https://github.test/octo-org/stacked-review/pull/1"
    )
    assert refreshed_state.changes[change_id].navigation_comment_id is None
    assert refreshed_state.changes[change_id].overview_comment_id is None


def test_view_reports_merged_pull_request_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"

    exit_code = run_main(repo, config_path, "view", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 merged into main, cleanup needed" in captured.out
    assert refreshed_state.changes[change_id].pr_state == "merged"
    assert refreshed_state.changes[change_id].pr_review_decision is None
