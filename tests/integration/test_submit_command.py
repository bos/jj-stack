from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_stack.errors import EXIT_CONFLICTS, EXIT_GITHUB, EXIT_USAGE
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_MARKER,
    is_overview_comment,
)
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import (
    FakeGithubState,
    create_app,
)
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
    write_file,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    patch_github_client_builders,
    read_remote_ref,
    remote_refs,
    run_main,
    write_config,
)


def _overview_comments(fake_repo, issue_number: int):
    return [
        comment
        for comment in issue_comments(fake_repo, issue_number)
        if is_overview_comment(comment.body)
    ]


def _assert_stack_pull_requests_match_dag(
    *,
    fake_repo,
    repo: Path,
    stack,
    trunk_branch: str = "main",
) -> None:
    state = ReviewStateStore.for_repo(repo).load()
    bookmarks_by_change: dict[str, str] = {}
    pull_requests_by_change = {}
    for revision in stack.revisions:
        identity = state.review_identities[revision.change_id]
        bookmark = identity.head_ref
        pr_number = identity.pr_number
        bookmarks_by_change[revision.change_id] = bookmark
        pull_requests_by_change[revision.change_id] = fake_repo.pull_requests[pr_number]
        assert read_remote_ref(fake_repo.git_dir, bookmark) == revision.commit_id

    for index, revision in enumerate(stack.revisions):
        pull_request = pull_requests_by_change[revision.change_id]
        expected_base = (
            bookmarks_by_change[stack.revisions[index - 1].change_id]
            if index > 0
            else trunk_branch
        )
        assert pull_request.title == revision.subject
        assert pull_request.state == "open"
        assert pull_request.merged_at is None
        assert pull_request.base_ref == expected_base


def test_submit_keeps_one_pr_ordinary_until_github_stack_is_needed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert tuple(fake_repo.pull_requests) == (1,)
    assert fake_repo.github_stacks == {}
    assert issue_comments(fake_repo, 1) == []

    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert tuple(fake_repo.pull_requests) == (1, 2)
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert all(issue_comments(fake_repo, number) == [] for number in (1, 2))


def test_submit_github_stack_recovers_lost_create_and_retries_blocked_append(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    appended: list[tuple[int, ...]] = []
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class LoseFirstCreateResponseClient(GithubClient):
        async def create_stack(self, *, pull_numbers):
            await super().create_stack(pull_numbers=pull_numbers)
            raise GithubClientError("Simulated lost response", status_code=500)

        async def append_to_stack(self, *, stack_number, pull_numbers):
            appended.append(tuple(pull_numbers))
            if len(appended) == 1:
                fake_repo.pull_requests[pull_numbers[0]].is_queued = True
            return await super().append_to_stack(
                stack_number=stack_number,
                pull_numbers=pull_numbers,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=LoseFirstCreateResponseClient,
    )
    state_store = ReviewStateStore.for_repo(repo)

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    assert "jj-stack submit" in capsys.readouterr().err
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert len(state_store.load().review_identities) == 2

    top_change_id = selected_stack(repo).revisions[-1].change_id
    run_command(
        ["jj", "describe", "-r", top_change_id, "-m", "feature 2 renamed\n\nupdated body"],
        repo,
    )
    stack_description = tmp_path / "stack.md"
    write_file(stack_description, "GitHub stack overview\n")

    assert run_main(repo, config_path, "submit", "--describe", f"stack={stack_description}") == 0
    assert fake_repo.pull_requests[2].title == "feature 2 renamed"
    assert fake_repo.pull_requests[2].body == "updated body"
    assert "GitHub stack overview" in _overview_comments(fake_repo, 2)[0].body

    for number in range(3, 6):
        commit_file(repo, f"feature {number}", f"feature-{number}.txt")
    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    assert fake_repo.github_stacks == {1: (1, 2)}
    fake_repo.pull_requests[3].is_queued = False
    assert run_main(repo, config_path, "submit") == 0

    assert (fake_repo.github_stacks, appended) == (
        {1: (1, 2, 3, 4, 5)},
        [(3, 4, 5), (3, 4, 5)],
    )


def test_submit_leaves_new_suffix_unsubmitted_while_an_ancestor_is_queued(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    pull_request = fake_repo.pull_requests[1]
    pull_request.is_queued = True
    remote_before = remote_refs(fake_repo.git_dir)
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")
    head_change_id = selected_stack(repo).head.change_id

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 1
    error = " ".join(captured.err.split())
    assert "is in the merge queue" in error
    assert "submit made no changes" in error
    assert "new changes above it remain unsubmitted" in error
    assert f"jj-stack sync {head_change_id}" in error
    assert f"jj-stack submit {head_change_id}" in error
    assert "remove PR #1 from the queue" not in error
    assert tuple(fake_repo.pull_requests) == (1,)
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert state_store.load() == state_before


def test_submit_recreates_github_stack_only_after_active_review_grows_to_two(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.apply_squash_merge(fake_repo.pull_requests[1])
    JjClient(repo).ensure_review_fetch_isolation(remote="origin")
    run_command(["jj", "git", "fetch", "--remote", "origin"], repo)
    active_change_id = selected_stack(repo).head.change_id
    run_command(["jj", "rebase", "-s", active_change_id, "-d", "main"], repo)

    exit_code = run_main(repo, config_path, "submit", active_change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert fake_repo.github_stacks == {}
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "open"
    assert fake_repo.pull_requests[2].base_ref == "main"

    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit") == 0
    assert fake_repo.github_stacks == {2: (2, 3)}


def test_submit_appends_to_active_suffix_after_historical_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.apply_squash_merge(fake_repo.pull_requests[1])
    fake_repo.update_pull_request_base(
        fake_repo.pull_requests[2],
        base_ref="main",
    )
    JjClient(repo).ensure_review_fetch_isolation(remote="origin")
    run_command(["jj", "git", "fetch", "--remote", "origin"], repo)
    active_change_id = selected_stack(repo).head.change_id
    run_command(["jj", "rebase", "-s", active_change_id, "-d", "main"], repo)
    commit_file(repo, "feature 3", "feature-3.txt")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert fake_repo.github_stacks == {7: (1, 2, 3)}
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[3].base_ref == fake_repo.pull_requests[2].head_ref


def test_submit_retargets_stale_review_bases_before_pushing_reordered_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")
    commit_file(repo, "feature 4", "feature-4.txt")

    initial_stack = selected_stack(repo)
    old_bottom_change_id = initial_stack.revisions[0].change_id
    old_top_change_id = initial_stack.revisions[-1].change_id

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "rebase", "-r", old_bottom_change_id, "-A", old_top_change_id], repo)
    reordered_stack = selected_stack(repo)

    assert run_main(repo, config_path, "submit", reordered_stack.head.change_id) == 0
    capsys.readouterr()

    refreshed_state = ReviewStateStore.for_repo(repo).load()
    bookmarks_by_subject = {
        revision.subject: refreshed_state.review_identities[revision.change_id].head_ref
        for revision in reordered_stack.revisions
    }
    assert all(pull_request.state == "open" for pull_request in fake_repo.pull_requests.values())
    assert (len(fake_repo.pull_requests), fake_repo.github_stacks) == (
        4,
        {2: (2, 3, 4, 1)},
    )
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[3].base_ref == bookmarks_by_subject["feature 2"]
    assert fake_repo.pull_requests[4].base_ref == bookmarks_by_subject["feature 3"]
    assert fake_repo.pull_requests[1].base_ref == bookmarks_by_subject["feature 4"]


def test_submit_stack_preflight_failures_recover_without_persisted_phase(
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
    failure = "membership"

    class PreflightFailureClient(GithubClient):
        async def list_stacks(self):
            if failure == "membership":
                raise GithubClientError("Simulated membership failure", status_code=500)
            return await super().list_stacks()

        async def unstack(self, *, stack_number):
            result = await super().unstack(stack_number=stack_number)
            if failure == "unstack":
                raise GithubClientError("Simulated lost unstack response", status_code=500)
            return result

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=PreflightFailureClient,
    )
    state_before = ReviewStateStore.for_repo(repo).load()
    remote_before = remote_refs(fake_repo.git_dir)

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    assert "Could not inspect GitHub repository" in capsys.readouterr().err

    # Reordering the stack makes the desired membership differ from the live one, so submit
    # must unstack the resource before it can move any branch or base.
    failure = "unstack"
    original = selected_stack(repo)
    run_command(
        ["jj", "rebase", "-r", original.revisions[0].change_id, "-A", original.head.change_id],
        repo,
    )
    reordered_head = selected_stack(repo).head.change_id
    state_before = ReviewStateStore.for_repo(repo).load()
    remote_before = remote_refs(fake_repo.git_dir)

    assert run_main(repo, config_path, "submit", "--dry-run", reordered_head) == 0
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert run_main(repo, config_path, "submit", reordered_head) == EXIT_GITHUB

    assert ReviewStateStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert fake_repo.github_stacks == {}

    failure = "none"
    assert run_main(repo, config_path, "submit", reordered_head) == 0
    assert ReviewStateStore.for_repo(repo).load().review_identities.keys() == (
        state_before.review_identities.keys()
    )
    assert fake_repo.github_stacks == {2: (2, 1)}


def test_submit_opens_new_pr_when_middle_change_is_split_in_two(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    write_file(repo / "feature-2a.txt", "alpha\n")
    write_file(repo / "feature-2b.txt", "beta\n")
    run_command(["jj", "describe", "-m", "feature 2"], repo)
    run_command(["jj", "new", "-m", "feature 3"], repo)
    write_file(repo / "feature-3.txt", "gamma\n")

    initial_stack = selected_stack(repo)
    original_middle_change_id = next(
        revision.change_id
        for revision in initial_stack.revisions
        if revision.subject == "feature 2"
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    initial_state = ReviewStateStore.for_repo(repo).load()
    original_middle_pr_number = initial_state.review_identities[
        original_middle_change_id
    ].pr_number

    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setenv("VISUAL", "true")
    monkeypatch.setenv("JJ_EDITOR", "true")
    run_command(
        ["jj", "split", "-r", original_middle_change_id, "feature-2a.txt"],
        repo,
    )

    split_stack = selected_stack(repo)
    assert len(split_stack.revisions) == 4
    assert split_stack.revisions[0].subject == "feature 1"
    assert split_stack.revisions[-1].subject == "feature 3"

    assert run_main(repo, config_path, "submit", split_stack.head.change_id) == 0
    capsys.readouterr()

    refreshed_state = ReviewStateStore.for_repo(repo).load()
    assert (
        refreshed_state.review_identities[original_middle_change_id].pr_number
        == original_middle_pr_number
    )
    pr_numbers = {
        refreshed_state.review_identities[revision.change_id].pr_number
        for revision in split_stack.revisions
    }
    assert len(pr_numbers) == 4
    assert all(fake_repo.pull_requests[pr_number].state == "open" for pr_number in pr_numbers)
    assert len(fake_repo.pull_requests) == 4


def test_submit_squash_blocks_until_old_github_stack_is_dissolved(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """`jj squash` plus auto-abandon collapses two reviewed changes; orphan survives."""

    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")

    initial_stack = selected_stack(repo)
    change_ids_by_subject = {
        revision.subject: revision.change_id for revision in initial_stack.revisions
    }

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    initial_state = ReviewStateStore.for_repo(repo).load()
    orphaned_identity = initial_state.review_identities[change_ids_by_subject["feature 2"]]
    orphaned_pr_number = orphaned_identity.pr_number
    orphaned_bookmark = orphaned_identity.head_ref
    orphaned_remote_target = read_remote_ref(fake_repo.git_dir, orphaned_bookmark)
    orphaned_pr_base_ref = fake_repo.pull_requests[orphaned_pr_number].base_ref

    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setenv("VISUAL", "true")
    monkeypatch.setenv("JJ_EDITOR", "true")
    run_command(
        [
            "jj",
            "squash",
            "--from",
            change_ids_by_subject["feature 2"],
            "--into",
            change_ids_by_subject["feature 1"],
        ],
        repo,
    )

    surviving_stack = selected_stack(repo, change_ids_by_subject["feature 3"])
    assert [revision.subject for revision in surviving_stack.revisions] == [
        "feature 1",
        "feature 3",
    ]
    exit_code = run_main(repo, config_path, "submit", surviving_stack.head.change_id)
    captured = capsys.readouterr()

    refreshed_state = ReviewStateStore.for_repo(repo).load()
    assert exit_code == 1
    assert "keeps #2 active outside the selected stack" in captured.err
    assert "jj-stack unstack --stack 1" in captured.err
    assert refreshed_state == initial_state
    orphaned_pr = fake_repo.pull_requests[orphaned_pr_number]
    assert orphaned_pr.state == "open"
    assert orphaned_pr.merged_at is None
    assert orphaned_pr.base_ref == orphaned_pr_base_ref
    assert read_remote_ref(fake_repo.git_dir, orphaned_bookmark) == orphaned_remote_target
    assert len(fake_repo.pull_requests) == 3


def test_submit_split_path_blocks_until_github_stack_is_dissolved(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A GitHub stack spanning two local paths must be dissolved before either is updated."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=4)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_ids = [revision.change_id for revision in selected_stack(repo).revisions]
    submitted_state = ReviewStateStore.for_repo(repo).load()
    deferred_change_id = change_ids[1]
    deferred_identity = submitted_state.review_identities[deferred_change_id]
    deferred_baseline = submitted_state.submitted_baselines[deferred_change_id]
    deferred_pull_request = fake_repo.pull_requests[deferred_identity.pr_number]
    shared_base_ref = deferred_pull_request.base_ref
    deferred_remote_target = read_remote_ref(fake_repo.git_dir, deferred_identity.head_ref)
    deferred_events = [
        event
        for event in fake_repo.pull_request_events
        if event.pull_request_number == deferred_identity.pr_number
    ]

    run_command(["jj", "rebase", "-s", change_ids[2], "-d", change_ids[0]], repo)
    fork_stack = selected_stack(repo, change_ids[3])
    assert [revision.change_id for revision in fork_stack.revisions] == [
        change_ids[0],
        change_ids[2],
        change_ids[3],
    ]

    exit_code = run_main(repo, config_path, "submit", change_ids[3])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "keeps #2 active outside the selected stack" in captured.err
    assert "jj-stack unstack --stack 1" in captured.err

    refreshed_state = ReviewStateStore.for_repo(repo).load()
    assert refreshed_state == submitted_state
    assert deferred_pull_request.base_ref == shared_base_ref
    assert deferred_pull_request.head_ref == deferred_identity.head_ref
    assert deferred_pull_request.state == "open"
    assert deferred_pull_request.merged_at is None
    assert read_remote_ref(fake_repo.git_dir, deferred_identity.head_ref) == (
        deferred_remote_target
    )
    assert refreshed_state.review_identities[deferred_change_id] == deferred_identity
    assert refreshed_state.submitted_baselines[deferred_change_id] == deferred_baseline
    assert [
        event
        for event in fake_repo.pull_request_events
        if event.pull_request_number == deferred_identity.pr_number
    ] == deferred_events
    assert len(fake_repo.pull_requests) == 4


def test_submit_uses_readable_review_branch_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    stack = selected_stack(repo)

    assert run_main(repo, config_path, "submit") == 0
    state = ReviewStateStore.for_repo(repo).load()
    assert JjClient(repo).visible_review_bookmark_targets() == {}

    for revision, subject in zip(stack.revisions, ("feature-1", "feature-2"), strict=True):
        branch = state.review_identities[revision.change_id].head_ref
        assert branch == f"jj-stack/{subject}-{revision.change_id[:8]}"
        assert f"refs/heads/{branch}" in remote_refs(fake_repo.git_dir)


def test_submit_draft_new_does_not_convert_published_pull_requests_back_to_draft(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    assert not fake_repo.pull_requests[1].is_draft

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id

    assert run_main(repo, config_path, "submit", "--draft=new", change_id) == 0
    capsys.readouterr()

    assert not fake_repo.pull_requests[1].is_draft


def test_submit_draft_all_converts_existing_published_stack_to_draft(
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
    assert fake_repo.pull_requests[1].is_draft is False
    assert fake_repo.pull_requests[2].is_draft is False

    stack = selected_stack(repo)
    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--draft=all",
        stack.revisions[-1].change_id,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "draft PR #1 updated" in captured.out
    assert "draft PR #2 updated" in captured.out
    assert fake_repo.pull_requests[1].is_draft
    assert fake_repo.pull_requests[2].is_draft


def test_submit_invalid_revset_reports_clean_error_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    exit_code = run_main(repo, config_path, "submit", "xporz")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error: Revision `xporz` doesn't exist" in captured.err
    assert "jj log --no-graph" not in captured.err
    empty_state = ReviewStateStore.for_repo(repo).load()
    assert empty_state.review_identities == {}
    assert empty_state.submitted_baselines == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_defaults_to_a_described_nonempty_working_copy(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "shared review", "shared.txt")
    shared = selected_stack(repo).head
    commit_file(repo, "committed path", "committed.txt")
    committed_path = selected_stack(repo).head
    run_command(["jj", "new", shared.change_id], repo)
    write_file(repo / "working-copy.txt", "working copy\n")
    run_command(["jj", "describe", "-m", "selected path"], repo)
    selected = JjClient(repo).resolve_revision("@")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()
    state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0, captured.err
    assert set(state.review_identities) == {shared.change_id, selected.change_id}
    assert committed_path.change_id not in state.review_identities
    assert len(fake_repo.pull_requests) == 2


def test_submit_blocks_unresolved_conflicted_rebase_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "shared.txt")

    stack = selected_stack(repo)
    change_id = stack.revisions[0].change_id

    run_command(["jj", "new", "main"], repo)
    write_file(repo / "shared.txt", "trunk 1\n")
    run_command(["jj", "commit", "-m", "trunk 1"], repo)
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    rebased_stack = selected_stack(repo, change_id)
    assert rebased_stack.revisions[0].conflict is True

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == EXIT_CONFLICTS
    assert "unresolved conflicts" in captured.err
    empty_state = ReviewStateStore.for_repo(repo).load()
    assert empty_state.review_identities == {}
    assert empty_state.submitted_baselines == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_describe_reads_pull_request_and_stack_bodies_from_files(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    stack = selected_stack(repo)
    first_description = tmp_path / "feature-1-pr.md"
    second_description = tmp_path / "feature-2-pr.md"
    stack_description = tmp_path / "stack.md"
    write_file(first_description, "First PR body\n\n- from file\n")
    write_file(second_description, "Second PR body\n\n- from file\n")
    write_file(stack_description, "Stack overview body\n\n- from file\n")
    monkeypatch.chdir(tmp_path)

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--describe",
        f"{stack.revisions[0].change_id}={first_description.name}",
        "--describe",
        f"{stack.revisions[1].commit_id}={second_description.name}",
        "--describe",
        f"stack={stack_description.name}",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Submitted changes:" in captured.out
    assert fake_repo.pull_requests[1].title == "feature 1"
    assert fake_repo.pull_requests[1].body == "First PR body\n\n- from file"
    assert fake_repo.pull_requests[2].title == "feature 2"
    assert fake_repo.pull_requests[2].body == "Second PR body\n\n- from file"
    assert len(_overview_comments(fake_repo, 2)) == 1
    assert STACK_OVERVIEW_COMMENT_MARKER in _overview_comments(fake_repo, 2)[0].body
    assert "Stack overview body\n\n- from file" in _overview_comments(fake_repo, 2)[0].body


def test_submit_describe_rejects_target_outside_selected_stack_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    description = tmp_path / "description.md"
    write_file(description, "Body that should not be submitted\n")

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--describe",
        f"trunk()={description}",
    )
    captured = capsys.readouterr()

    assert exit_code == EXIT_USAGE
    assert "--describe target trunk() is not in the selected stack" in captured.err
    empty_state = ReviewStateStore.for_repo(repo).load()
    assert empty_state.review_identities == {}
    assert empty_state.submitted_baselines == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}
    assert issue_comments(fake_repo, 1) == []


def test_submit_describe_with_generates_pull_request_and_stack_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    helper = tmp_path / "describe.py"
    helper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "from pathlib import Path",
                "import sys",
                "",
                "stack_input_env = 'JJ_STACK_INPUT_FILE'",
                "kind, revset = sys.argv[1], sys.argv[2]",
                "if kind == '--pr':",
                "    payload = {",
                "        'title': f'AI {revset[:8]}',",
                "        'body': f'Generated body for {revset}',",
                "    }",
                "elif kind == '--stack':",
                "    stack_input = json.loads(",
                "        Path(os.environ[stack_input_env]).read_text(encoding='utf-8')",
                "    )",
                "    revisions = stack_input['revisions']",
                "    payload = {",
                "        'title': 'Generated stack summary',",
                "        'body': (",
                '            f"Generated stack body for {revset}: "',
                "            f\"{revisions[0]['title']} -> {revisions[1]['title']} | \"",
                "            f\"{revisions[0]['diffstat'].splitlines()[0]}\"",
                "        ),",
                "    }",
                "else:",
                "    raise SystemExit(f'unexpected args: {sys.argv[1:]}')",
                "print(json.dumps(payload))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--describe-with",
        str(helper),
    )
    captured = capsys.readouterr()
    stack = selected_stack(repo)

    assert exit_code == 0
    assert "Submitted changes:" in captured.out
    assert fake_repo.pull_requests[1].title == f"AI {stack.revisions[0].change_id[:8]}"
    assert fake_repo.pull_requests[1].body == (
        f"Generated body for {stack.revisions[0].change_id}"
    )
    assert fake_repo.pull_requests[2].title == f"AI {stack.revisions[1].change_id[:8]}"
    assert fake_repo.pull_requests[2].body == (
        f"Generated body for {stack.revisions[1].change_id}"
    )
    assert len(_overview_comments(fake_repo, 2)) == 1
    assert STACK_OVERVIEW_COMMENT_MARKER in _overview_comments(fake_repo, 2)[0].body
    assert "## Generated stack summary" in _overview_comments(fake_repo, 2)[0].body
    assert (
        f"Generated stack body for {stack.selected_revset}: "
        f"AI {stack.revisions[0].change_id[:8]} -> AI {stack.revisions[1].change_id[:8]} | "
        "feature-1.txt" in _overview_comments(fake_repo, 2)[0].body
    )


def test_submit_describe_with_failure_aborts_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    helper = tmp_path / "describe.py"
    helper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('not json')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--describe-with",
        str(helper),
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "returned invalid JSON" in captured.err
    empty_state = ReviewStateStore.for_repo(repo).load()
    assert empty_state.review_identities == {}
    assert empty_state.submitted_baselines == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}
    assert issue_comments(fake_repo, 1) == []


def test_submit_dry_run_does_not_mutate_local_remote_or_github_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    JjClient(repo).ensure_review_fetch_isolation(remote="origin")

    initial_remote_refs = remote_refs(fake_repo.git_dir)

    exit_code = run_main(repo, config_path, "submit", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "Planned changes:" in captured.out
    assert "feature 1" in captured.out
    assert ": new PR" in captured.out
    assert fake_repo.pull_requests == {}
    assert remote_refs(fake_repo.git_dir) == initial_remote_refs


def test_submit_dry_run_reports_update_without_mutating_remote_or_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    state_before = ReviewStateStore.for_repo(repo).load()
    remote_refs_before = remote_refs(fake_repo.git_dir)

    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    exit_code = run_main(repo, config_path, "submit", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "pushed, PR #1 updated" in captured.out
    assert "PR #1 updated" in captured.out
    assert fake_repo.pull_requests[1].title == "feature 1"
    assert remote_refs(fake_repo.git_dir) == remote_refs_before
    assert ReviewStateStore.for_repo(repo).load() == state_before


@pytest.mark.parametrize(
    ("rewrite", "tracked"),
    ((False, False), (True, False), (True, True)),
)
def test_submit_accepts_a_matching_visible_review_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
    rewrite: bool,
    tracked: bool,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = ReviewStateStore.for_repo(repo).load()
    change_id, identity = next(iter(state.review_identities.items()))
    old_commit = state.submitted_baselines[change_id].commit_id
    if rewrite:
        run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)
    if tracked:
        run_command(["jj", "bookmark", "track", f"{identity.head_ref}@origin"], repo)

    assert identity.head_ref in JjClient(repo).visible_review_bookmark_targets()
    assert run_main(repo, config_path, "submit", change_id) == 0
    assert "divergent changes are not supported" not in capsys.readouterr().err

    submitted = ReviewStateStore.for_repo(repo).load().submitted_baselines[change_id].commit_id
    assert read_remote_ref(fake_repo.git_dir, identity.head_ref) == submitted
    assert (submitted != old_commit) is rewrite


def test_submit_rejects_a_conflicted_visible_review_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = ReviewStateStore.for_repo(repo).load()
    change_id, identity = next(iter(state.review_identities.items()))
    old_commit = state.submitted_baselines[change_id].commit_id
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    rewritten = selected_stack(repo, change_id).head.commit_id
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)
    run_command(["jj", "bookmark", "create", identity.head_ref, "-r", rewritten], repo)

    assert run_main(repo, config_path, "submit", change_id) != 0
    assert "divergent" in capsys.readouterr().err
    assert read_remote_ref(fake_repo.git_dir, identity.head_ref) == old_commit


@pytest.mark.parametrize(
    ("rewrite", "other_bookmark", "expected"),
    (
        (False, False, "immutable commits"),
        (True, False, "divergent changes are not supported"),
        (True, True, "divergent changes are not supported"),
    ),
)
def test_visible_review_bookmark_preserves_other_immutability(
    tmp_path: Path,
    monkeypatch,
    capsys,
    rewrite: bool,
    other_bookmark: bool,
    expected: str,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = ReviewStateStore.for_repo(repo).load()
    change_id = next(iter(state.review_identities))
    baseline = state.submitted_baselines[change_id].commit_id
    if rewrite:
        run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    if other_bookmark:
        run_command(
            [
                "git",
                "--git-dir",
                str(fake_repo.git_dir),
                "update-ref",
                "refs/heads/other-review-copy",
                baseline,
            ],
            repo,
        )
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)
    if not other_bookmark:
        run_command(
            [
                "jj",
                "config",
                "set",
                "--repo",
                'revset-aliases."immutable_heads()"',
                f"builtin_immutable_heads() | {baseline}",
            ],
            repo,
        )

    assert run_main(repo, config_path, "submit", change_id) == 2
    assert expected in capsys.readouterr().err


def test_submit_does_not_claim_a_visible_bookmark_for_an_untracked_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    revision = selected_stack(repo).head
    branch = f"jj-stack/feature-1-{revision.change_id[:8]}"
    run_command(["jj", "bookmark", "create", branch, "-r", revision.commit_id], repo)

    assert run_main(repo, config_path, "submit", "--dry-run", revision.change_id) == 1
    assert f"Cannot claim visible bookmark {branch}" in capsys.readouterr().err
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}


def test_submit_batches_stack_overview_comment_reads_with_graphql(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    comment_batch_calls: list[tuple[int, ...]] = []

    class CountingCommentLookupClient(GithubClient):
        async def get_issue_comments_by_pull_request_numbers(
            self,
            *,
            pull_numbers: Sequence[int],
        ):
            comment_batch_calls.append(tuple(sorted(pull_numbers)))
            return await super().get_issue_comments_by_pull_request_numbers(
                pull_numbers=pull_numbers,
            )

        async def list_issue_comments(
            self,
            *,
            issue_number: int,
        ):
            raise AssertionError(
                f"submit should batch overview comment reads for pull request #{issue_number}"
            )

    app = create_app(FakeGithubState.single_repository(fake_repo))
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=CountingCommentLookupClient,
    )

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code == 0
    assert comment_batch_calls == [(1, 2)]


def test_submit_moves_overview_comment_when_stack_head_advances(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    helper = tmp_path / "describe.py"
    helper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import sys",
                "",
                "kind, revset = sys.argv[1], sys.argv[2]",
                "if kind == '--pr':",
                "    print(json.dumps({'title': revset[:8], 'body': revset}))",
                "elif kind == '--stack':",
                "    print(json.dumps({'title': 'stack', 'body': 'stack body'}))",
                "else:",
                "    raise SystemExit(f'unexpected args: {sys.argv[1:]}')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    assert run_main(repo, config_path, "submit", "--describe-with", str(helper)) == 0
    capsys.readouterr()
    initial_stack = selected_stack(repo)
    initial_top_change_id = initial_stack.revisions[-1].change_id
    initial_top_pr_number = (
        ReviewStateStore.for_repo(repo).load().review_identities[initial_top_change_id].pr_number
    )
    assert len(_overview_comments(fake_repo, initial_top_pr_number)) == 1

    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit", "--describe-with", str(helper)) == 0
    capsys.readouterr()
    refreshed_stack = selected_stack(repo)
    new_top_change_id = refreshed_stack.revisions[-1].change_id
    refreshed_state = ReviewStateStore.for_repo(repo).load()
    new_top_pr_number = refreshed_state.review_identities[new_top_change_id].pr_number

    assert _overview_comments(fake_repo, initial_top_pr_number) == []
    assert len(_overview_comments(fake_repo, new_top_pr_number)) == 1


def test_submit_single_change_clears_stale_stack_overview_comment(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstale stack overview",
        issue_number=1,
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert issue_comments(fake_repo, 1) == []


def test_submit_rejects_ambiguous_stack_overview_comments(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\none",
        issue_number=2,
    )
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\ntwo",
        issue_number=2,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple jj-stack stack overview comments" in captured.err


def test_submit_reports_stack_overview_comment_update_failures_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nold overview",
        issue_number=2,
    )
    stack_description = tmp_path / "stack.md"
    write_file(stack_description, "New stack overview\n")

    class FailingCommentUpdateClient(GithubClient):
        async def update_issue_comment(
            self,
            *,
            comment_id: int,
            body: str,
        ):
            raise GithubClientError("GitHub request failed: 404 Not Found", status_code=404)

    app = create_app(FakeGithubState.single_repository(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=FailingCommentUpdateClient,
    )

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        change_id,
        "--describe",
        f"stack={stack_description}",
    )
    captured = capsys.readouterr()

    assert exit_code == EXIT_GITHUB
    assert "Could not update stack overview comment" in captured.err
    assert "Traceback" not in captured.err


def test_submit_reports_up_to_date_when_remote_branch_and_pr_already_match(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    first_refs = remote_refs(fake_repo.git_dir)
    first_prs = {
        number: pull_request.title for number, pull_request in fake_repo.pull_requests.items()
    }

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "already pushed" in captured.out
    assert "unchanged" in captured.out
    assert remote_refs(fake_repo.git_dir) == first_refs
    assert {number: pr.title for number, pr in fake_repo.pull_requests.items()} == first_prs


def test_submit_updates_existing_remote_review_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    identity = ReviewStateStore.for_repo(repo).load().review_identities[change_id]
    bookmark = identity.head_ref
    pr_number = identity.pr_number

    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = selected_stack(repo, change_id)

    assert exit_code == 0
    assert "pushed" in captured.out
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.revisions[-1].commit_id
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"
    assert fake_repo.pull_requests[pr_number].body == "feature 1 renamed"


def test_submit_rerun_recovers_after_lost_remote_update_response(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    identity = ReviewStateStore.for_repo(repo).load().review_identities[change_id]
    bookmark = identity.head_ref
    pr_number = identity.pr_number

    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    original_mutate = JjClient.mutate_remote_review_refs

    def mutate_then_fail(
        self,
        *,
        remote: str,
        updates,
    ) -> None:
        original_mutate(self, remote=remote, updates=updates)
        raise RuntimeError("Simulated failure after remote update")

    monkeypatch.setattr(
        "jj_stack.commands.submit.command.JjClient.mutate_remote_review_refs",
        mutate_then_fail,
    )

    with pytest.raises(RuntimeError, match="Simulated failure after remote update"):
        run_main(repo, config_path, "submit", change_id)
    capsys.readouterr()

    monkeypatch.setattr(
        "jj_stack.commands.submit.command.JjClient.mutate_remote_review_refs",
        original_mutate,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = selected_stack(repo, change_id)

    assert exit_code == 0
    assert "updated" in captured.out
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.revisions[-1].commit_id
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"


@pytest.mark.merge_recovery
def test_submit_requires_relink_after_state_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    identity = state_store.load().review_identities[change_id]
    bookmark = identity.head_ref
    pr_number = identity.pr_number

    state_path = resolve_state_path(repo)
    state_path.unlink()
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    assert run_main(repo, config_path, "submit", change_id) == 1
    rejected = capsys.readouterr()
    assert "Adopt that PR explicitly with relink" in rejected.err

    assert run_main(repo, config_path, "relink", str(pr_number), change_id) == 0
    capsys.readouterr()
    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = selected_stack(repo, change_id)
    rewritten_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.pull_requests) == {pr_number}
    assert rewritten_state.review_identities[change_id].head_ref == bookmark
    assert rewritten_state.review_identities[change_id].pr_number == pr_number
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.revisions[-1].commit_id
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"


@pytest.mark.merge_recovery
def test_submit_names_sync_when_tracked_review_is_merged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id

    def reject_editor(**_kwargs) -> None:
        raise AssertionError("submit opened the editor before rejecting the merged PR")

    monkeypatch.setattr(
        "jj_stack.commands.submit.command.edit_pull_requests_in_editor",
        reject_editor,
    )

    fake_repo.apply_squash_merge(fake_repo.pull_requests[1])
    exit_code = run_main(repo, config_path, "submit", "--edit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert f"jj-stack sync {change_id}" in captured.err
    assert "relink" not in captured.err


def test_submit_fails_closed_when_cached_pull_request_is_missing_on_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.review_identities[change_id].head_ref
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)

    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Saved pull request link exists" in captured.err
    assert "view" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests == {}


def test_submit_fails_closed_when_github_reports_multiple_pull_requests(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.review_identities[change_id].head_ref
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)
    fake_repo.create_pull_request(
        base_ref="main",
        body="duplicate",
        head_ref=bookmark,
        title="feature 1 duplicate",
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple pull requests" in captured.err
    assert "view" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert set(fake_repo.pull_requests) == {1, 2}


def test_submit_fails_closed_when_saved_remote_branch_drifted_externally(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = selected_stack(repo)
    middle_change_id = stack.revisions[1].change_id
    top_change_id = stack.revisions[2].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    middle_bookmark = initial_state.review_identities[middle_change_id].head_ref
    top_target = initial_state.submitted_baselines[top_change_id].commit_id

    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{middle_bookmark}",
            top_target,
        ],
        fake_repo.git_dir.parent,
    )
    drifted_refs = remote_refs(fake_repo.git_dir)
    pull_requests_before = {
        number: (
            pull_request.base_ref,
            pull_request.head_ref,
            pull_request.state,
            pull_request.merged_at,
            pull_request.title,
            pull_request.body,
        )
        for number, pull_request in fake_repo.pull_requests.items()
    }
    fake_repo.pull_request_events.clear()

    exit_code = run_main(repo, config_path, "submit", middle_change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unexpected commit" in captured.err
    assert state_store.load() == initial_state
    assert remote_refs(fake_repo.git_dir) == drifted_refs
    assert {
        number: (
            pull_request.base_ref,
            pull_request.head_ref,
            pull_request.state,
            pull_request.merged_at,
            pull_request.title,
            pull_request.body,
        )
        for number, pull_request in fake_repo.pull_requests.items()
    } == pull_requests_before
    assert fake_repo.pull_request_events == []


def test_submit_accepts_stack_forked_from_trunk_ancestor(
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
    stack = selected_stack(repo)

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()
    state = ReviewStateStore.for_repo(repo).load()
    change_id = stack.revisions[-1].change_id
    bookmark = state.review_identities[change_id].head_ref

    assert exit_code == 0
    assert "Submitted changes:" in captured.out
    assert stack.revisions[-1].subject in captured.out
    assert len(fake_repo.pull_requests) == 1
    assert fake_repo.pull_requests[1].base_ref == "main"
    assert read_remote_ref(fake_repo.git_dir, bookmark) == stack.revisions[-1].commit_id


def test_submit_open_marks_existing_draft_pull_requests_ready_for_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit", "--draft") == 0
    draft_output = capsys.readouterr().out
    stack = selected_stack(repo)
    change_id = stack.revisions[-1].change_id

    assert "draft PR #1" in draft_output
    assert fake_repo.pull_requests[1].is_draft is True

    exit_code = run_main(repo, config_path, "submit", "--open", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert not fake_repo.pull_requests[1].is_draft


def test_submit_checkpoints_successful_in_flight_pull_request_before_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    stack = selected_stack(repo)
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailSpecificPullRequestClient(GithubClient):
        async def create_pull_request(
            self,
            *,
            base,
            body,
            draft=False,
            head,
            title,
        ):
            if title == "feature 2":
                await asyncio.sleep(0.01)
                raise GithubClientError(
                    "Simulated failure for feature 2",
                    status_code=500,
                )
            if title == "feature 1":
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
        client_type=FailSpecificPullRequestClient,
    )

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code != 0

    state = ReviewStateStore.for_repo(repo).load()
    assert state.review_identities[change_id_1].pr_number == 1
    assert change_id_1 in state.submitted_baselines
    assert change_id_2 not in state.review_identities
    assert change_id_2 not in state.submitted_baselines
    assert len(fake_repo.pull_requests) == 1 and fake_repo.github_stacks == {}
    assert fake_repo.pull_requests[1].title == "feature 1"
    pushed_review_refs = {
        ref: target
        for ref, target in remote_refs(fake_repo.git_dir).items()
        if ref.startswith("refs/heads/jj-stack/")
    }
    assert len(pushed_review_refs) == 2
    assert set(pushed_review_refs.values()) == {
        revision.commit_id for revision in stack.revisions
    }


def test_submit_rerun_converges_pull_request_metadata_after_partial_create_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_config(
        tmp_path,
        fake_repo,
        extra_lines=[
            'labels = ["needs-review"]',
            'reviewers = ["alice"]',
            'team_reviewers = ["platform"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repository(fake_repo))
    metadata_failure_injected = False

    class FlakyMetadataClient(GithubClient):
        async def add_labels(self, *, issue_number, labels):
            nonlocal metadata_failure_injected
            if not metadata_failure_injected:
                metadata_failure_injected = True
                raise GithubClientError(
                    "Simulated label failure",
                    status_code=500,
                )
            await super().add_labels(
                issue_number=issue_number,
                labels=labels,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command", "jj_stack.commands.relink"),
        client_type=FlakyMetadataClient,
    )

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    capsys.readouterr()

    state_after_failure = ReviewStateStore.for_repo(repo).load()
    assert len(fake_repo.pull_requests) == 1
    assert state_after_failure.review_identities == {}
    assert state_after_failure.submitted_baselines == {}
    assert fake_repo.pull_requests[1].requested_reviewers == ["alice"]
    assert fake_repo.pull_requests[1].requested_team_reviewers == ["platform"]
    assert fake_repo.pull_requests[1].labels == []

    stack = selected_stack(repo)
    change_id = stack.revisions[0].change_id
    assert run_main(repo, config_path, "submit") == 1
    rejected = capsys.readouterr()
    assert "Adopt that PR explicitly with relink" in rejected.err

    assert run_main(repo, config_path, "relink", "1", change_id) == 0
    capsys.readouterr()
    assert run_main(repo, config_path, "submit", "--reviewers", "alice") == 0
    capsys.readouterr()

    state_after_rerun = ReviewStateStore.for_repo(repo).load()

    assert state_after_rerun.review_identities[change_id].pr_number == 1
    assert fake_repo.pull_requests[1].requested_reviewers == ["alice"]
    assert fake_repo.pull_requests[1].requested_team_reviewers == ["platform"]
    assert fake_repo.pull_requests[1].labels == ["needs-review"]


def test_submit_unchanged_rerun_skips_pull_request_metadata_writes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_config(
        tmp_path,
        fake_repo,
        extra_lines=[
            'labels = ["needs-review"]',
            'reviewers = ["alice"]',
            'team_reviewers = ["platform"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    metadata_write_calls: list[str] = []

    class NoMetadataWritesClient(GithubClient):
        async def request_reviewers(
            self,
            *,
            pull_number,
            reviewers,
            team_reviewers,
        ) -> None:
            metadata_write_calls.append("reviewers")
            raise AssertionError("unchanged rerun should not request reviewers")

        async def add_labels(self, *, issue_number, labels) -> None:
            metadata_write_calls.append("labels")
            raise AssertionError("unchanged rerun should not add labels")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
        client_type=NoMetadataWritesClient,
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert metadata_write_calls == []


def test_submit_explicit_reviewers_apply_to_unchanged_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_config(tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--reviewers",
            "alice,bob",
            "--team-reviewers",
            "platform",
        )
        == 0
    )
    capsys.readouterr()

    pull_request = fake_repo.pull_requests[1]
    assert pull_request.requested_reviewers == ["alice", "bob"]
    assert pull_request.requested_team_reviewers == ["platform"]


def test_submit_re_request_adds_prior_approved_reviewer_through_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_config(
        tmp_path,
        fake_repo,
        extra_lines=[
            'reviewers = ["pending-reviewer"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="alice",
        state="APPROVED",
    )

    assert run_main(repo, config_path, "submit", "--re-request") == 0
    capsys.readouterr()

    assert fake_repo.pull_requests[1].requested_reviewers == [
        "pending-reviewer",
        "alice",
    ]


def _write_edit_editor(tmp_path: Path, name: str, body_lines: list[str]) -> str:
    import sys as _sys

    editor = tmp_path / name
    editor.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    return f"{_sys.executable} {editor}"


def test_submit_edit_malformed_document_aborts_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    editor_command = _write_edit_editor(
        tmp_path,
        "truncate-descriptions.py",
        [
            "from pathlib import Path",
            "import sys",
            "",
            "path = Path(sys.argv[-1])",
            "lines = path.read_text(encoding='utf-8').splitlines()",
            "separators = [",
            "    index",
            "    for index, line in enumerate(lines)",
            "    if line.startswith('====== change ')",
            "]",
            "path.write_text(",
            "    '\\n'.join(lines[: separators[1]]) + '\\n', encoding='utf-8'",
            ")",
        ],
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", editor_command)

    exit_code = run_main(repo, config_path, "submit", "--edit")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "missing change" in captured.err
    empty_state = ReviewStateStore.for_repo(repo).load()
    assert empty_state.review_identities == {}
    assert empty_state.submitted_baselines == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_edit_sets_each_pull_request_draft_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    editor_command = _write_edit_editor(
        tmp_path,
        "toggle-first-draft.py",
        [
            "from pathlib import Path",
            "import sys",
            "",
            "path = Path(sys.argv[-1])",
            "text = path.read_text(encoding='utf-8')",
            "path.write_text(",
            "    text.replace('JJ: Draft: yes', 'JJ: Draft: n', 1),",
            "    encoding='utf-8',",
            ")",
        ],
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", editor_command)

    assert run_main(repo, config_path, "submit", "--draft", "--edit") == 0
    capsys.readouterr()

    assert fake_repo.pull_requests[1].is_draft
    assert not fake_repo.pull_requests[2].is_draft

    editor_command = _write_edit_editor(
        tmp_path,
        "reverse-drafts.py",
        [
            "from pathlib import Path",
            "import sys",
            "",
            "path = Path(sys.argv[-1])",
            "text = path.read_text(encoding='utf-8')",
            "text = text.replace('JJ: Draft: no', 'JJ: Draft: y')",
            "text = text.replace('JJ: Draft: yes', 'JJ: Draft: n')",
            "path.write_text(text, encoding='utf-8')",
        ],
    )
    monkeypatch.setenv("EDITOR", editor_command)

    assert run_main(repo, config_path, "submit", "--edit") == 0
    capsys.readouterr()

    assert not fake_repo.pull_requests[1].is_draft
    assert fake_repo.pull_requests[2].is_draft
