from __future__ import annotations

import asyncio
from pathlib import Path

from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
)
from .submit_command_helpers import (
    configure_submit_environment,
    patch_github_client_builders,
    run_main,
)


def test_submit_restart_creates_fresh_pr_from_saved_readable_branch_name(
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
    expected_branch = (
        stale_bookmark.removesuffix(f"-{short_change_id(change_id)}")
        + f"-fresh-pr1-{short_change_id(change_id)}"
    )

    assert exit_code == 0
    assert restarted_identity.pr_number == 2
    assert restarted_identity.head_ref == expected_branch
    assert fake_repo.pull_requests[1].head_ref == generated_bookmark
    assert fake_repo.pull_requests[2].head_ref == expected_branch


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
    fake_repo.create_pull_request(
        base_ref="main",
        body="collision",
        head_ref=fresh_bookmark,
        title="collision",
    )

    exit_code = run_main(repo, config_path, "submit", "--restart", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Cannot recover the restart" in captured.err
    assert "relink" in captured.err
    assert "intended replacement" in captured.err
    assert state_store.load() == state


def test_submit_restart_dry_run_rejects_live_replacement_branch_drift(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    revision = JjClient(repo).discover_review_stack().head
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    identity = state.review_identities[revision.change_id]
    short_id = short_change_id(revision.change_id)
    fresh_bookmark = identity.head_ref.removesuffix(f"-{short_id}") + f"-fresh-pr1-{short_id}"
    wrong_commit = run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "commit-tree",
            "refs/heads/main^{tree}",
            "-p",
            "refs/heads/main",
            "-m",
            "external candidate",
        ],
        repo,
    ).stdout.strip()
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{fresh_bookmark}",
            wrong_commit,
        ],
        repo,
    )
    fake_repo.create_pull_request(
        base_ref="main",
        body="interrupted restart",
        head_ref=fresh_bookmark,
        title=revision.subject,
    )
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class StalePullRequestHeadClient(GithubClient):
        async def get_pull_requests_by_head_refs(self, *, head_refs):
            found = await super().get_pull_requests_by_head_refs(head_refs=head_refs)
            candidate = found[fresh_bookmark][0]
            found[fresh_bookmark] = (
                candidate.model_copy(
                    update={"head": candidate.head.model_copy(update={"sha": revision.commit_id})}
                ),
            )
            return found

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=StalePullRequestHeadClient,
    )

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--dry-run",
        "--restart",
        revision.change_id,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already exists and points to another change" in captured.err
    assert state_store.load() == state
    assert set(fake_repo.pull_requests) == {1, 2}


def test_submit_restart_reuses_partial_prs_before_replacing_all_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    old_state = state_store.load()
    app = create_app(FakeGithubState.single_repository(fake_repo))
    fail_top = True

    class FailTopRestartClient(GithubClient):
        async def create_pull_request(
            self,
            *,
            base,
            body,
            draft=False,
            head,
            title,
        ):
            if fail_top and title == "feature 2":
                await asyncio.sleep(0.01)
                raise GithubClientError("Simulated failure for feature 2", status_code=500)
            await asyncio.sleep(0.03)
            return await super().create_pull_request(
                base=base,
                body=body,
                draft=draft,
                head=head,
                title=title,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=FailTopRestartClient,
    )

    exit_code = run_main(repo, config_path, "submit", "--restart", stack.head.change_id)
    capsys.readouterr()

    assert exit_code != 0
    assert state_store.load() == old_state
    assert set(fake_repo.pull_requests) == {1, 2, 3}

    fail_top = False
    assert run_main(repo, config_path, "submit", "--restart", stack.head.change_id) == 0
    capsys.readouterr()
    restarted_state = state_store.load()

    assert restarted_state.review_identities[bottom_change_id].pr_number == 3
    assert restarted_state.review_identities[top_change_id].pr_number == 4
    assert set(fake_repo.pull_requests) == {1, 2, 3, 4}


def test_submit_restart_reuses_replacements_after_comment_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    old_state = state_store.load()
    app = create_app(FakeGithubState.single_repository(fake_repo))
    fail_comments = True

    class FailCommentClient(GithubClient):
        async def create_issue_comment(self, *, issue_number, body):
            if fail_comments:
                raise GithubClientError("Simulated comment failure", status_code=500)
            return await super().create_issue_comment(issue_number=issue_number, body=body)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=FailCommentClient,
    )

    assert run_main(repo, config_path, "submit", "--restart", stack.head.change_id) != 0
    capsys.readouterr()
    assert state_store.load() == old_state
    assert set(fake_repo.pull_requests) == {1, 2, 3, 4}

    fail_comments = False
    assert run_main(repo, config_path, "submit", "--restart", stack.head.change_id) == 0
    capsys.readouterr()
    assert {identity.pr_number for identity in state_store.load().review_identities.values()} == {
        3,
        4,
    }
    assert set(fake_repo.pull_requests) == {1, 2, 3, 4}


def test_submit_restart_preserves_old_tracking_when_final_pr_base_drifts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    old_state = state_store.load()
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class DriftFinalObservationClient(GithubClient):
        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            numbers = tuple(pull_numbers)
            if numbers == (2,):
                fake_repo.pull_requests[2].base_ref = "unexpected-base"
            return await super().get_pull_requests_by_numbers(pull_numbers=numbers)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=DriftFinalObservationClient,
    )

    assert run_main(repo, config_path, "submit", "--restart", change_id) == 1
    captured = capsys.readouterr()

    assert "Cannot save the restarted review" in captured.err
    assert "relink 2" in captured.err
    assert state_store.load() == old_state
    assert set(fake_repo.pull_requests) == {1, 2}


def test_repeated_submit_restart_replaces_restart_marker_in_readable_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = JjClient(repo).discover_review_stack().head.change_id
    short_id = short_change_id(change_id)
    state_store = ReviewStateStore.for_repo(repo)
    original_branch = state_store.load().review_identities[change_id].head_ref
    stem = original_branch.removesuffix(f"-{short_id}")

    assert run_main(repo, config_path, "submit", "--restart", change_id) == 0
    capsys.readouterr()
    first_restart = state_store.load().review_identities[change_id]
    assert first_restart.head_ref == f"{stem}-fresh-pr1-{short_id}"

    assert run_main(repo, config_path, "submit", "--restart", change_id) == 0
    capsys.readouterr()
    second_restart = state_store.load().review_identities[change_id]

    assert second_restart.pr_number == 3
    assert second_restart.head_ref == f"{stem}-fresh-pr2-{short_id}"
    assert "fresh-pr1-fresh-pr2" not in second_restart.head_ref
    assert fake_repo.pull_requests[3].head_ref == second_restart.head_ref
