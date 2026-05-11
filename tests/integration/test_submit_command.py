from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.stack_comments import (
    STACK_NAVIGATION_COMMENT_MARKER,
    STACK_OVERVIEW_COMMENT_MARKER,
    is_navigation_comment,
    is_overview_comment,
)
from jj_review.jj import JjClient
from jj_review.models.github import GithubPullRequest
from jj_review.state.journal import read_operation_log
from jj_review.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import (
    FakeGithubState,
    create_app,
)
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
    write_file,
)
from .submit_command_helpers import (
    approve_pull_requests,
    configure_submit_environment,
    issue_comments,
    patch_github_client_builders,
    read_remote_ref,
    remote_refs,
    run_main,
    write_config,
)


def _navigation_comments(fake_repo, issue_number: int):
    return [
        comment
        for comment in issue_comments(fake_repo, issue_number)
        if is_navigation_comment(comment.body)
    ]


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
        cached_change = state.changes[revision.change_id]
        bookmark = cached_change.bookmark
        pr_number = cached_change.pr_number
        assert bookmark is not None
        assert pr_number is not None
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


def test_submit_projects_review_bookmarks_to_selected_remote(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    state = ReviewStateStore.for_repo(repo).load()
    first_bookmark = state.changes[stack.revisions[0].change_id].bookmark
    top_pr_url = state.changes[stack.revisions[-1].change_id].pr_url

    assert exit_code == 0
    assert (
        f"Selected: {stack.head.subject} ({short_change_id(stack.head.change_id)})"
        in captured.out
    )
    assert "Submitted changes:" in captured.out
    assert ": main" not in captured.out
    assert top_pr_url is not None
    assert f"Top of stack: {top_pr_url}" in captured.out
    assert captured.out.index("feature 2") < captured.out.index("feature 1")
    assert captured.out.index("feature 1") < captured.out.index(stack.trunk.subject)
    assert len(fake_repo.pull_requests) == 2
    for index, revision in enumerate(stack.revisions, start=1):
        cached_change = state.changes[revision.change_id]
        bookmark = cached_change.bookmark
        assert bookmark is not None
        assert cached_change.pr_number == index
        assert cached_change.pr_state == "open"
        assert (
            cached_change.pr_url
            == fake_repo.pull_requests[index].to_payload(
                repository=fake_repo,
                web_origin="https://github.test",
            )["html_url"]
        )
        assert read_remote_ref(fake_repo.git_dir, bookmark) == revision.commit_id

    assert fake_repo.pull_requests[1].base_ref == "main"
    assert fake_repo.pull_requests[2].base_ref == first_bookmark


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

    initial_stack = JjClient(repo).discover_review_stack()
    old_bottom_change_id = initial_stack.revisions[0].change_id
    old_top_change_id = initial_stack.revisions[-1].change_id

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "rebase", "-r", old_bottom_change_id, "-A", old_top_change_id], repo)
    reordered_stack = JjClient(repo).discover_review_stack()

    assert [revision.subject for revision in reordered_stack.revisions] == [
        "feature 2",
        "feature 3",
        "feature 4",
        "feature 1",
    ]
    assert run_main(repo, config_path, "submit", reordered_stack.head.change_id) == 0
    capsys.readouterr()

    refreshed_state = ReviewStateStore.for_repo(repo).load()
    bookmarks_by_subject = {
        revision.subject: refreshed_state.changes[revision.change_id].bookmark
        for revision in reordered_stack.revisions
    }
    pull_requests_by_title = {
        pull_request.title: pull_request for pull_request in fake_repo.pull_requests.values()
    }

    assert all(pull_request.state == "open" for pull_request in fake_repo.pull_requests.values())
    assert all(
        pull_request.merged_at is None for pull_request in fake_repo.pull_requests.values()
    )
    assert pull_requests_by_title["feature 2"].base_ref == "main"
    assert pull_requests_by_title["feature 3"].base_ref == bookmarks_by_subject["feature 2"]
    assert pull_requests_by_title["feature 4"].base_ref == bookmarks_by_subject["feature 3"]
    assert pull_requests_by_title["feature 1"].base_ref == bookmarks_by_subject["feature 4"]


def test_submit_preserves_prs_when_middle_change_moves_to_stack_top(
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

    initial_stack = JjClient(repo).discover_review_stack()
    change_ids_by_subject = {
        revision.subject: revision.change_id for revision in initial_stack.revisions
    }

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(
        [
            "jj",
            "rebase",
            "-r",
            change_ids_by_subject["feature 2"],
            "-A",
            change_ids_by_subject["feature 4"],
        ],
        repo,
    )
    moved_stack = JjClient(repo).discover_review_stack()

    assert [revision.subject for revision in moved_stack.revisions] == [
        "feature 1",
        "feature 3",
        "feature 4",
        "feature 2",
    ]
    assert run_main(repo, config_path, "submit", moved_stack.head.change_id) == 0
    capsys.readouterr()

    _assert_stack_pull_requests_match_dag(
        fake_repo=fake_repo,
        repo=repo,
        stack=moved_stack,
    )
    assert len(fake_repo.pull_requests) == 4


def test_submit_preserves_existing_prs_when_change_is_inserted_in_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")

    initial_stack = JjClient(repo).discover_review_stack()
    change_ids_by_subject = {
        revision.subject: revision.change_id for revision in initial_stack.revisions
    }

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "new", change_ids_by_subject["feature 1"]], repo)
    commit_file(repo, "feature inserted", "feature-inserted.txt")
    inserted_change_id = JjClient(repo).discover_review_stack().head.change_id
    run_command(
        [
            "jj",
            "rebase",
            "-s",
            change_ids_by_subject["feature 2"],
            "-d",
            inserted_change_id,
        ],
        repo,
    )
    inserted_stack = JjClient(repo).discover_review_stack(change_ids_by_subject["feature 3"])

    assert [revision.subject for revision in inserted_stack.revisions] == [
        "feature 1",
        "feature inserted",
        "feature 2",
        "feature 3",
    ]
    assert run_main(repo, config_path, "submit", inserted_stack.head.change_id) == 0
    capsys.readouterr()

    _assert_stack_pull_requests_match_dag(
        fake_repo=fake_repo,
        repo=repo,
        stack=inserted_stack,
    )
    assert len(fake_repo.pull_requests) == 4


def test_submit_preserves_orphaned_pr_when_middle_change_is_abandoned(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")

    initial_stack = JjClient(repo).discover_review_stack()
    change_ids_by_subject = {
        revision.subject: revision.change_id for revision in initial_stack.revisions
    }

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    initial_state = ReviewStateStore.for_repo(repo).load()
    orphaned_change = initial_state.changes[change_ids_by_subject["feature 2"]]
    orphaned_bookmark = orphaned_change.bookmark
    orphaned_pr_number = orphaned_change.pr_number
    assert orphaned_bookmark is not None
    assert orphaned_pr_number is not None
    orphaned_remote_target = read_remote_ref(fake_repo.git_dir, orphaned_bookmark)

    run_command(["jj", "abandon", change_ids_by_subject["feature 2"]], repo)
    surviving_stack = JjClient(repo).discover_review_stack(change_ids_by_subject["feature 3"])

    assert [revision.subject for revision in surviving_stack.revisions] == [
        "feature 1",
        "feature 3",
    ]
    assert run_main(repo, config_path, "submit", surviving_stack.head.change_id) == 0
    capsys.readouterr()

    _assert_stack_pull_requests_match_dag(
        fake_repo=fake_repo,
        repo=repo,
        stack=surviving_stack,
    )
    assert len(fake_repo.pull_requests) == 3
    assert fake_repo.pull_requests[orphaned_pr_number].state == "open"
    assert fake_repo.pull_requests[orphaned_pr_number].merged_at is None
    assert read_remote_ref(fake_repo.git_dir, orphaned_bookmark) == orphaned_remote_target


def test_submit_preserves_prs_when_top_change_moves_to_stack_bottom(
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

    initial_stack = JjClient(repo).discover_review_stack()
    change_ids_by_subject = {
        revision.subject: revision.change_id for revision in initial_stack.revisions
    }

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(
        [
            "jj",
            "rebase",
            "-r",
            change_ids_by_subject["feature 4"],
            "-B",
            change_ids_by_subject["feature 1"],
        ],
        repo,
    )
    moved_stack = JjClient(repo).discover_review_stack()

    assert [revision.subject for revision in moved_stack.revisions] == [
        "feature 4",
        "feature 1",
        "feature 2",
        "feature 3",
    ]
    assert run_main(repo, config_path, "submit", moved_stack.head.change_id) == 0
    capsys.readouterr()

    _assert_stack_pull_requests_match_dag(
        fake_repo=fake_repo,
        repo=repo,
        stack=moved_stack,
    )
    assert len(fake_repo.pull_requests) == 4


def test_submit_preserves_prs_when_adjacent_changes_are_swapped(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")

    initial_stack = JjClient(repo).discover_review_stack()
    change_ids_by_subject = {
        revision.subject: revision.change_id for revision in initial_stack.revisions
    }

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(
        [
            "jj",
            "rebase",
            "-r",
            change_ids_by_subject["feature 2"],
            "-A",
            change_ids_by_subject["feature 3"],
        ],
        repo,
    )
    swapped_stack = JjClient(repo).discover_review_stack()

    assert [revision.subject for revision in swapped_stack.revisions] == [
        "feature 1",
        "feature 3",
        "feature 2",
    ]
    assert run_main(repo, config_path, "submit", swapped_stack.head.change_id) == 0
    capsys.readouterr()

    _assert_stack_pull_requests_match_dag(
        fake_repo=fake_repo,
        repo=repo,
        stack=swapped_stack,
    )
    assert len(fake_repo.pull_requests) == 3


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

    initial_stack = JjClient(repo).discover_review_stack()
    original_middle_change_id = next(
        revision.change_id
        for revision in initial_stack.revisions
        if revision.subject == "feature 2"
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    initial_state = ReviewStateStore.for_repo(repo).load()
    original_middle_pr_number = initial_state.changes[original_middle_change_id].pr_number
    assert original_middle_pr_number is not None

    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setenv("VISUAL", "true")
    monkeypatch.setenv("JJ_EDITOR", "true")
    run_command(
        ["jj", "split", "-r", original_middle_change_id, "feature-2a.txt"],
        repo,
    )

    split_stack = JjClient(repo).discover_review_stack()
    assert len(split_stack.revisions) == 4
    assert split_stack.revisions[0].subject == "feature 1"
    assert split_stack.revisions[-1].subject == "feature 3"

    assert run_main(repo, config_path, "submit", split_stack.head.change_id) == 0
    capsys.readouterr()

    refreshed_state = ReviewStateStore.for_repo(repo).load()
    assert (
        refreshed_state.changes[original_middle_change_id].pr_number
        == original_middle_pr_number
    )
    pr_numbers = {
        refreshed_state.changes[revision.change_id].pr_number
        for revision in split_stack.revisions
    }
    assert None not in pr_numbers
    assert len(pr_numbers) == 4
    assert all(
        fake_repo.pull_requests[pr_number].state == "open"
        for pr_number in pr_numbers
        if pr_number is not None
    )
    assert len(fake_repo.pull_requests) == 4


def test_submit_preserves_orphan_when_two_adjacent_changes_are_squashed(
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

    initial_stack = JjClient(repo).discover_review_stack()
    change_ids_by_subject = {
        revision.subject: revision.change_id for revision in initial_stack.revisions
    }

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    initial_state = ReviewStateStore.for_repo(repo).load()
    surviving_pr_number = initial_state.changes[change_ids_by_subject["feature 1"]].pr_number
    orphaned_change = initial_state.changes[change_ids_by_subject["feature 2"]]
    orphaned_pr_number = orphaned_change.pr_number
    orphaned_bookmark = orphaned_change.bookmark
    assert surviving_pr_number is not None
    assert orphaned_pr_number is not None
    assert orphaned_bookmark is not None
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

    surviving_stack = JjClient(repo).discover_review_stack(
        change_ids_by_subject["feature 3"]
    )
    assert [revision.subject for revision in surviving_stack.revisions] == [
        "feature 1",
        "feature 3",
    ]
    assert run_main(repo, config_path, "submit", surviving_stack.head.change_id) == 0
    capsys.readouterr()

    _assert_stack_pull_requests_match_dag(
        fake_repo=fake_repo,
        repo=repo,
        stack=surviving_stack,
    )

    refreshed_state = ReviewStateStore.for_repo(repo).load()
    assert (
        refreshed_state.changes[change_ids_by_subject["feature 1"]].pr_number
        == surviving_pr_number
    )
    orphaned_pr = fake_repo.pull_requests[orphaned_pr_number]
    assert orphaned_pr.state == "open"
    assert orphaned_pr.merged_at is None
    assert orphaned_pr.base_ref == orphaned_pr_base_ref
    assert read_remote_ref(fake_repo.git_dir, orphaned_bookmark) == orphaned_remote_target
    assert len(fake_repo.pull_requests) == 3


def test_submit_post_flight_check_catches_unexpected_pull_request_closure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """If the pre-push predictor misses an auto-close, the post-flight check fires."""

    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")
    commit_file(repo, "feature 4", "feature-4.txt")

    initial_stack = JjClient(repo).discover_review_stack()
    old_bottom_change_id = initial_stack.revisions[0].change_id
    old_top_change_id = initial_stack.revisions[-1].change_id

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "rebase", "-r", old_bottom_change_id, "-A", old_top_change_id], repo)
    reordered_stack = JjClient(repo).discover_review_stack()

    from jj_review.commands.submit import auto_close as submit_auto_close

    monkeypatch.setattr(
        submit_auto_close,
        "predict_pull_requests_auto_closed_by_push",
        lambda **_kwargs: (),
    )

    assert run_main(repo, config_path, "submit", reordered_stack.head.change_id) != 0
    captured = capsys.readouterr()

    assert "Pull request(s) #2 were open at the start of this submit" in captured.err
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None


def test_submit_post_flight_check_catches_vanished_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A PR open at start but no longer reported by GitHub at end fails closed."""

    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "describe", "-m", "feature 2 updated"], repo)

    original_get_pull_requests_by_numbers = GithubClient.get_pull_requests_by_numbers

    async def get_pull_requests_by_numbers_with_vanish(
        self, owner, repo, *, pull_numbers
    ):
        result = await original_get_pull_requests_by_numbers(
            self, owner, repo, pull_numbers=pull_numbers
        )
        if 2 in result:
            result[2] = None
        return result

    monkeypatch.setattr(
        GithubClient,
        "get_pull_requests_by_numbers",
        get_pull_requests_by_numbers_with_vanish,
    )

    assert run_main(repo, config_path, "submit") != 0
    captured = capsys.readouterr()

    assert (
        "Pull request(s) #2 were open at the start of this submit but GitHub "
        "no longer reports them"
    ) in captured.err
    assert "deleted or transferred" in captured.err


def test_submit_uses_configured_bookmark_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=['bookmark_prefix = "bosullivan"'],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    state = ReviewStateStore.for_repo(repo).load()

    bookmarks = [
        cached_change.bookmark
        for cached_change in state.changes.values()
        if cached_change.bookmark is not None
    ]
    assert len(bookmarks) == 2
    assert all(bookmark.startswith("bosullivan/") for bookmark in bookmarks)
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)
        for bookmark in bookmarks
    )


def test_submit_uses_configured_use_bookmarks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=['use_bookmarks = ["potato/*", "spam/eggs"]'],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    stack = JjClient(repo).discover_review_stack()
    run_command(
        ["jj", "bookmark", "create", "potato/feature-1", "-r", stack.revisions[0].commit_id], repo
    )
    run_command(
        ["jj", "bookmark", "create", "spam/eggs", "-r", stack.revisions[1].commit_id], repo
    )

    assert run_main(repo, config_path, "submit") == 0
    state = ReviewStateStore.for_repo(repo).load()

    assert state.changes[stack.revisions[0].change_id].bookmark == "potato/feature-1"
    assert state.changes[stack.revisions[0].change_id].bookmark_ownership == "external"
    assert state.changes[stack.revisions[1].change_id].bookmark == "spam/eggs"
    assert state.changes[stack.revisions[1].change_id].bookmark_ownership == "external"
    assert "refs/heads/potato/feature-1" in remote_refs(fake_repo.git_dir)
    assert "refs/heads/spam/eggs" in remote_refs(fake_repo.git_dir)


def test_submit_cli_use_bookmarks_overrides_configured_patterns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=['use_bookmarks = ["wrong/*"]'],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    stack = JjClient(repo).discover_review_stack()
    run_command(
        ["jj", "bookmark", "create", "potato/feature-1", "-r", stack.revisions[0].commit_id], repo
    )

    assert run_main(repo, config_path, "submit", "--use-bookmarks=potato/*") == 0
    state = ReviewStateStore.for_repo(repo).load()

    assert state.changes[stack.revisions[0].change_id].bookmark == "potato/feature-1"
    assert state.changes[stack.revisions[0].change_id].bookmark_ownership == "external"

def test_submit_draft_creates_draft_pull_requests_and_persists_draft_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    exit_code = run_main(repo, config_path, "submit", "--draft")
    captured = capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    cached_change = ReviewStateStore.for_repo(repo).load().changes[change_id]

    assert exit_code == 0
    assert "draft PR #1" in captured.out
    assert fake_repo.pull_requests[1].is_draft
    assert cached_change.pr_is_draft is True
    assert cached_change.pr_state == "open"


def test_submit_draft_new_does_not_convert_published_pull_requests_back_to_draft(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    assert not fake_repo.pull_requests[1].is_draft

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id

    assert run_main(repo, config_path, "submit", "--draft=new", change_id) == 0
    capsys.readouterr()

    assert not fake_repo.pull_requests[1].is_draft
    assert ReviewStateStore.for_repo(repo).load().changes[change_id].pr_is_draft is False


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

    stack = JjClient(repo).discover_review_stack()
    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--draft=all",
        stack.revisions[-1].change_id,
    )
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "draft PR #1 updated" in captured.out
    assert "draft PR #2 updated" in captured.out
    assert fake_repo.pull_requests[1].is_draft
    assert fake_repo.pull_requests[2].is_draft
    assert refreshed_state.changes[stack.revisions[0].change_id].pr_is_draft is True
    assert refreshed_state.changes[stack.revisions[1].change_id].pr_is_draft is True


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
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_blocks_unresolved_conflicted_rebase_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "shared.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id

    run_command(["jj", "new", "main"], repo)
    write_file(repo / "shared.txt", "trunk 1\n")
    run_command(["jj", "commit", "-m", "trunk 1"], repo)
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    rebased_stack = JjClient(repo).discover_review_stack(change_id)
    assert rebased_stack.revisions[0].conflict is True

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unresolved conflicts" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_creates_navigation_comment_for_each_pull_request_in_multi_pr_stack(
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
    bottom_change_id = stack.revisions[0].change_id
    top_change_id = stack.revisions[-1].change_id
    state = ReviewStateStore.for_repo(repo).load()

    assert _overview_comments(fake_repo, 1) == []
    assert _overview_comments(fake_repo, 2) == []
    assert len(_navigation_comments(fake_repo, 1)) == 1
    assert len(_navigation_comments(fake_repo, 2)) == 1
    assert STACK_NAVIGATION_COMMENT_MARKER in _navigation_comments(fake_repo, 1)[0].body
    assert "**feature 1 (this PR)**" in _navigation_comments(fake_repo, 1)[0].body
    assert "[feature 2](https://github.test/octo-org/stacked-review/pull/2)" in (
        _navigation_comments(fake_repo, 1)[0].body
    )
    assert "trunk `main`" in _navigation_comments(fake_repo, 1)[0].body
    assert "**feature 2 (this PR)**" in _navigation_comments(fake_repo, 2)[0].body
    assert "[feature 1](https://github.test/octo-org/stacked-review/pull/1)" in (
        _navigation_comments(fake_repo, 2)[0].body
    )
    assert state.changes[bottom_change_id].navigation_comment_id is not None
    assert state.changes[bottom_change_id].overview_comment_id is None
    assert state.changes[top_change_id].navigation_comment_id is not None
    assert state.changes[top_change_id].overview_comment_id is None


def test_submit_skips_stack_comment_for_single_commit_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state = ReviewStateStore.for_repo(repo).load()

    assert issue_comments(fake_repo, 1) == []
    assert fake_repo.pull_requests[1].body == "feature 1"
    assert state.changes[change_id].navigation_comment_id is None
    assert state.changes[change_id].overview_comment_id is None


def test_submit_persists_topology_pointers_for_each_change(
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
    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    middle_change_id = stack.revisions[1].change_id
    top_change_id = stack.revisions[-1].change_id
    state = ReviewStateStore.for_repo(repo).load()

    assert state.changes[bottom_change_id].last_submitted_parent_change_id is None
    assert state.changes[middle_change_id].last_submitted_parent_change_id == bottom_change_id
    assert state.changes[top_change_id].last_submitted_parent_change_id == middle_change_id
    for change_id in (bottom_change_id, middle_change_id, top_change_id):
        assert state.changes[change_id].last_submitted_stack_head_change_id == top_change_id


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
                "stack_input_env = 'JJ_REVIEW_STACK_INPUT_FILE'",
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
    stack = JjClient(repo).discover_review_stack()

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
    assert len(_navigation_comments(fake_repo, 1)) == 1
    assert len(_navigation_comments(fake_repo, 2)) == 1
    assert len(_overview_comments(fake_repo, 2)) == 1
    assert STACK_OVERVIEW_COMMENT_MARKER in _overview_comments(fake_repo, 2)[0].body
    assert "## Generated stack summary" in _overview_comments(fake_repo, 2)[0].body
    assert (
        f"Generated stack body for {stack.selected_revset}: "
        f"AI {stack.revisions[0].change_id[:8]} -> AI {stack.revisions[1].change_id[:8]} | "
        "feature-1.txt" in _overview_comments(fake_repo, 2)[0].body
    )
    assert "This pull request is part of a stack tracked by `jj-review`." in (
        _navigation_comments(fake_repo, 2)[0].body
    )


def test_submit_describe_with_skips_stack_helper_for_single_commit_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    helper = tmp_path / "describe.py"
    log_path = tmp_path / "helper.log"
    helper.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import pathlib",
                "import sys",
                "",
                f"log_path = pathlib.Path({str(log_path)!r})",
                "log_path.write_text(",
                "    log_path.read_text() + ' '.join(sys.argv[1:]) + '\\n' if log_path.exists()",
                "    else ' '.join(sys.argv[1:]) + '\\n'",
                ")",
                "kind, revset = sys.argv[1], sys.argv[2]",
                "if kind != '--pr':",
                "    raise SystemExit(f'unexpected args: {sys.argv[1:]}')",
                "print(json.dumps({'title': f'AI {revset[:8]}', 'body': f'Body {revset}'}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--describe-with",
            str(helper),
        )
        == 0
    )
    capsys.readouterr()
    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state = ReviewStateStore.for_repo(repo).load()

    assert fake_repo.pull_requests[1].title == f"AI {change_id[:8]}"
    assert log_path.read_text(encoding="utf-8").splitlines() == [f"--pr {change_id}"]
    assert issue_comments(fake_repo, 1) == []
    assert state.changes[change_id].navigation_comment_id is None
    assert state.changes[change_id].overview_comment_id is None


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
    assert ReviewStateStore.for_repo(repo).load().changes == {}
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


def test_submit_dry_run_skips_github_for_never_tracked_local_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    class NoGithubReadsClient(GithubClient):
        async def get_repository(self, owner: str, repo: str):
            raise AssertionError("dry-run should not load the GitHub repository")

        async def get_pull_requests_by_head_refs(
            self,
            owner: str,
            repo: str,
            *,
            head_refs: Sequence[str],
        ) -> dict[str, tuple[GithubPullRequest, ...]]:
            raise AssertionError(
                f"dry-run should not discover pull requests for {head_refs!r}"
            )

        async def list_issue_comments(
            self,
            owner: str,
            repo: str,
            *,
            issue_number: int,
        ):
            raise AssertionError(
                f"dry-run should not inspect stack comments for pull request #{issue_number}"
            )

    app = create_app(FakeGithubState.single_repository(fake_repo))
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
        client_type=NoGithubReadsClient,
    )

    exit_code = run_main(repo, config_path, "submit", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "Planned changes:" in captured.out
    assert fake_repo.pull_requests == {}


def test_submit_dry_run_reports_update_without_mutating_remote_or_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
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


def test_submit_dry_run_skips_stack_comment_github_reads(
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

    class NoCommentReadsClient(GithubClient):
        async def list_issue_comments(
            self,
            owner: str,
            repo: str,
            *,
            issue_number: int,
        ):
            raise AssertionError(
                f"dry-run should not inspect stack comments for pull request #{issue_number}"
            )

    app = create_app(FakeGithubState.single_repository(fake_repo))
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
        client_type=NoCommentReadsClient,
    )

    exit_code = run_main(repo, config_path, "submit", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "Planned changes:" in captured.out


def test_submit_batches_stack_comment_reads_with_graphql(
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

    comment_batch_calls: list[tuple[int, ...]] = []

    class CountingCommentLookupClient(GithubClient):
        async def get_issue_comments_by_pull_request_numbers(
            self,
            owner: str,
            repo: str,
            *,
            pull_numbers: Sequence[int],
        ):
            comment_batch_calls.append(tuple(sorted(pull_numbers)))
            return await super().get_issue_comments_by_pull_request_numbers(
                owner,
                repo,
                pull_numbers=pull_numbers,
            )

        async def list_issue_comments(
            self,
            owner: str,
            repo: str,
            *,
            issue_number: int,
        ):
            raise AssertionError(
                f"submit should batch stack comment reads for pull request #{issue_number}"
            )

    app = create_app(FakeGithubState.single_repository(fake_repo))
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
        client_type=CountingCommentLookupClient,
    )

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code == 0
    assert comment_batch_calls == [(1, 2)]


def test_submit_rediscovers_and_regenerates_stack_comments_when_cache_is_missing(
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
    approve_pull_requests(fake_repo, 1, 2)

    stack = JjClient(repo).discover_review_stack()
    top_change_id = stack.revisions[-1].change_id
    bottom_change_id = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    initial_comment_id = initial_state.changes[top_change_id].navigation_comment_id
    assert initial_comment_id is not None

    _navigation_comments(fake_repo, 2)[0].body = (
        f"{STACK_NAVIGATION_COMMENT_MARKER}\nmanually edited"
    )
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    top_change_id: initial_state.changes[top_change_id].model_copy(
                        update={"navigation_comment_id": None}
                    ),
                }
            }
        )
    )

    run_command(["jj", "describe", "-r", top_change_id, "-m", "feature 2 renamed"], repo)

    assert run_main(repo, config_path, "submit", top_change_id) == 0
    capsys.readouterr()
    refreshed_state = state_store.load()

    assert len(_navigation_comments(fake_repo, 2)) == 1
    assert _navigation_comments(fake_repo, 2)[0].id == initial_comment_id
    assert "**feature 2 renamed (this PR)**" in _navigation_comments(fake_repo, 2)[0].body
    assert "[feature 1](https://github.test/octo-org/stacked-review/pull/1)" in (
        _navigation_comments(fake_repo, 2)[0].body
    )
    assert len(_navigation_comments(fake_repo, 1)) == 1
    assert refreshed_state.changes[top_change_id].navigation_comment_id == initial_comment_id
    assert refreshed_state.changes[bottom_change_id].navigation_comment_id is not None


def test_submit_moves_managed_stack_comment_to_new_selected_head(
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
    initial_stack = JjClient(repo).discover_review_stack()
    old_top_change_id = initial_stack.revisions[-1].change_id
    old_bottom_change_id = initial_stack.revisions[0].change_id
    initial_state = ReviewStateStore.for_repo(repo).load()
    initial_comment_id = initial_state.changes[old_top_change_id].navigation_comment_id

    assert initial_comment_id is not None
    assert len(_navigation_comments(fake_repo, 1)) == 1
    assert len(_navigation_comments(fake_repo, 2)) == 1

    commit_file(repo, "feature 3", "feature-3.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    refreshed_stack = JjClient(repo).discover_review_stack()
    refreshed_state = ReviewStateStore.for_repo(repo).load()
    new_top_change_id = refreshed_stack.revisions[-1].change_id

    assert len(_navigation_comments(fake_repo, 1)) == 1
    assert len(_navigation_comments(fake_repo, 2)) == 1
    assert len(_navigation_comments(fake_repo, 3)) == 1
    assert refreshed_state.changes[old_bottom_change_id].navigation_comment_id is not None
    assert refreshed_state.changes[old_top_change_id].navigation_comment_id is not None
    assert refreshed_state.changes[new_top_change_id].navigation_comment_id is not None
    assert refreshed_state.changes[new_top_change_id].navigation_comment_id != initial_comment_id


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
    initial_stack = JjClient(repo).discover_review_stack()
    initial_top_change_id = initial_stack.revisions[-1].change_id
    initial_top_pr_number = (
        ReviewStateStore.for_repo(repo).load().changes[initial_top_change_id].pr_number
    )
    assert initial_top_pr_number is not None
    assert len(_overview_comments(fake_repo, initial_top_pr_number)) == 1

    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit", "--describe-with", str(helper)) == 0
    capsys.readouterr()
    refreshed_stack = JjClient(repo).discover_review_stack()
    new_top_change_id = refreshed_stack.revisions[-1].change_id
    refreshed_state = ReviewStateStore.for_repo(repo).load()
    new_top_pr_number = refreshed_state.changes[new_top_change_id].pr_number
    assert new_top_pr_number is not None

    assert _overview_comments(fake_repo, initial_top_pr_number) == []
    assert refreshed_state.changes[initial_top_change_id].overview_comment_id is None
    assert len(_overview_comments(fake_repo, new_top_pr_number)) == 1
    assert refreshed_state.changes[new_top_change_id].overview_comment_id is not None


def test_submit_single_change_clears_stale_managed_stack_comment(
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
    manual_comment = fake_repo.create_issue_comment(
        body=f"{STACK_NAVIGATION_COMMENT_MARKER}\nstale stack navigation",
        issue_number=1,
    )
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"navigation_comment_id": manual_comment.id}
                    ),
                }
            }
        )
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    refreshed_state = state_store.load()

    assert issue_comments(fake_repo, 1) == []
    assert refreshed_state.changes[change_id].navigation_comment_id is None


def test_submit_rejects_cached_stack_comment_id_for_non_stack_comment(
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
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    manual_comment = fake_repo.create_issue_comment(body="manual note", issue_number=2)
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"navigation_comment_id": manual_comment.id}
                    ),
                }
            }
        )
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "does not belong to jj-review" in captured.err
    assert manual_comment in issue_comments(fake_repo, 2)


def test_submit_rejects_ambiguous_discovered_stack_comments(
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
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    fake_repo.create_issue_comment(
        body=f"{STACK_NAVIGATION_COMMENT_MARKER}\nextra",
        issue_number=2,
    )
    state_store.save(
        initial_state.model_copy(
            update={
                "changes": {
                    **initial_state.changes,
                    change_id: initial_state.changes[change_id].model_copy(
                        update={"navigation_comment_id": None}
                    ),
                }
            }
        )
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple jj-review stack navigation comments" in captured.err


def test_submit_reports_stack_comment_update_failures_without_traceback(
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
    change_id = stack.revisions[-1].change_id
    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    class FailingCommentUpdateClient(GithubClient):
        async def update_issue_comment(
            self,
            owner: str,
            repo: str,
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
        modules=("jj_review.commands.submit.command",),
        client_type=FailingCommentUpdateClient,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not update stack navigation comment" in captured.err
    assert "Traceback" not in captured.err


def test_submit_reports_up_to_date_when_remote_bookmark_and_pr_already_match(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    first_output = capsys.readouterr().out
    first_refs = remote_refs(fake_repo.git_dir)
    first_prs = {
        number: pull_request.title for number, pull_request in fake_repo.pull_requests.items()
    }

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1" in first_output
    assert "already pushed" in captured.out
    assert "unchanged" in captured.out
    assert remote_refs(fake_repo.git_dir) == first_refs
    assert {number: pr.title for number, pr in fake_repo.pull_requests.items()} == first_prs


def test_submit_rejects_unlinked_change_until_relink(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "unlinked from review tracking" in captured.err
    assert "relink" in captured.err


def test_submit_updates_existing_pull_request_after_change_rewrite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    write_file(repo / "feature-2.txt", "feature 2\n")
    write_file(repo / "details.txt", "more detail\n")
    run_command(["jj", "commit", "-m", "feature 2\n\nbody line"], repo)
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    first_stack = JjClient(repo).discover_review_stack()
    top_change_id = first_stack.revisions[-1].change_id
    initial_bookmark = ReviewStateStore.for_repo(repo).load().changes[top_change_id].bookmark
    assert initial_bookmark is not None
    initial_pr_number = ReviewStateStore.for_repo(repo).load().changes[top_change_id].pr_number
    assert initial_pr_number is not None

    run_command(
        ["jj", "describe", "-r", top_change_id, "-m", "feature 2 renamed\n\nupdated body"],
        repo,
    )

    exit_code = run_main(repo, config_path, "submit", top_change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(top_change_id)
    rewritten_state = ReviewStateStore.for_repo(repo).load()
    rewritten_bookmark = rewritten_state.changes[top_change_id].bookmark

    assert exit_code == 0
    assert rewritten_bookmark == initial_bookmark
    assert "updated" in captured.out
    assert (
        read_remote_ref(fake_repo.git_dir, initial_bookmark)
        == rewritten_stack.revisions[-1].commit_id
    )
    assert fake_repo.pull_requests[initial_pr_number].title == "feature 2 renamed"
    assert fake_repo.pull_requests[initial_pr_number].body == "updated body"


def test_submit_updates_existing_untracked_remote_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    cached_change = ReviewStateStore.for_repo(repo).load().changes[change_id]
    bookmark = cached_change.bookmark
    pr_number = cached_change.pr_number
    assert bookmark is not None
    assert pr_number is not None

    run_command(["jj", "bookmark", "forget", bookmark], repo)
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)
    bookmark_state = JjClient(repo).get_bookmark_state(bookmark)
    remote_state = bookmark_state.remote_target("origin")

    assert exit_code == 0
    assert "pushed" in captured.out
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.revisions[-1].commit_id
    assert remote_state is not None
    assert remote_state.is_tracked is True
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"
    assert fake_repo.pull_requests[pr_number].body == "feature 1 renamed"


def test_submit_rerun_recovers_after_failure_following_untracked_remote_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    cached_change = ReviewStateStore.for_repo(repo).load().changes[change_id]
    bookmark = cached_change.bookmark
    pr_number = cached_change.pr_number
    assert bookmark is not None
    assert pr_number is not None

    run_command(["jj", "bookmark", "forget", bookmark], repo)
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    original_update_untracked_remote_bookmark = JjClient.update_untracked_remote_bookmark

    def update_untracked_remote_bookmark_then_fail(
        self,
        *,
        remote: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        original_update_untracked_remote_bookmark(
            self,
            remote=remote,
            bookmark=bookmark,
            desired_target=desired_target,
            expected_remote_target=expected_remote_target,
        )
        raise RuntimeError("Simulated failure after untracked remote update")

    monkeypatch.setattr(
        "jj_review.commands.submit.command.JjClient.update_untracked_remote_bookmark",
        update_untracked_remote_bookmark_then_fail,
    )

    with pytest.raises(RuntimeError, match="Simulated failure after untracked remote update"):
        run_main(repo, config_path, "submit", change_id)
    capsys.readouterr()

    bookmark_state = JjClient(repo).get_bookmark_state(bookmark)
    remote_state = bookmark_state.remote_target("origin")
    assert remote_state is not None
    assert remote_state.is_tracked is True

    monkeypatch.setattr(
        "jj_review.commands.submit.command.JjClient.update_untracked_remote_bookmark",
        original_update_untracked_remote_bookmark,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)

    assert exit_code == 0
    assert "updated" in captured.out
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.revisions[-1].commit_id
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"


def test_submit_rediscovers_review_branch_after_state_and_local_bookmark_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    cached_change = state_store.load().changes[change_id]
    bookmark = cached_change.bookmark
    pr_number = cached_change.pr_number
    assert bookmark is not None
    assert pr_number is not None

    state_path = resolve_state_path(repo)
    state_path.unlink()
    run_command(["jj", "bookmark", "forget", bookmark], repo)
    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = JjClient(repo).discover_review_stack(change_id)
    rewritten_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.pull_requests) == {pr_number}
    assert rewritten_state.changes[change_id].bookmark == bookmark
    assert rewritten_state.changes[change_id].pr_number == pr_number
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.revisions[-1].commit_id
    assert fake_repo.pull_requests[pr_number].title == "feature 1 renamed"


def test_submit_fails_closed_when_cached_pull_request_is_missing_on_github(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)

    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Saved pull request link exists" in captured.err
    assert "status --fetch" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests == {}


def test_submit_fails_closed_when_saved_pull_request_number_differs_for_head_branch(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)

    del fake_repo.pull_requests[1]
    fake_repo.create_pull_request(
        base_ref="main",
        body="feature 1",
        head_ref=bookmark,
        title="feature 1 reopened",
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Saved pull request #1 does not match" in captured.err
    assert "status --fetch" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert set(fake_repo.pull_requests) == {2}


def test_submit_fails_closed_when_github_reports_multiple_pull_requests(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
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
    assert "status --fetch" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert set(fake_repo.pull_requests) == {1, 2}


def test_submit_fails_closed_when_github_reports_closed_pull_request_for_head_branch(
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
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)
    fake_repo.pull_requests[1].state = "closed"

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "in state closed" in captured.err
    assert "status --fetch" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests[1].state == "closed"


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

    stack = JjClient(repo).discover_review_stack()
    middle_change_id = stack.revisions[1].change_id
    top_change_id = stack.revisions[2].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    middle_bookmark = initial_state.changes[middle_change_id].bookmark
    top_target = initial_state.changes[top_change_id].last_submitted_commit_id
    assert middle_bookmark is not None
    assert top_target is not None

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


def test_submit_reports_no_reviewable_commits_without_mutation_when_head_is_trunk(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack("main")

    exit_code = run_main(repo, config_path, "submit", "main")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert stack.trunk.subject in captured.out
    assert "The selected stack has no changes to review." in captured.out
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


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
    stack = JjClient(repo).discover_review_stack(allow_immutable=True)

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()
    state = ReviewStateStore.for_repo(repo).load()
    change_id = stack.revisions[-1].change_id
    bookmark = state.changes[change_id].bookmark

    assert exit_code == 0
    assert "Submitted changes:" in captured.out
    assert stack.revisions[-1].subject in captured.out
    assert len(fake_repo.pull_requests) == 1
    assert fake_repo.pull_requests[1].base_ref == "main"
    assert bookmark is not None
    assert read_remote_ref(fake_repo.git_dir, bookmark) == stack.revisions[-1].commit_id


def test_submit_rejects_ambiguous_use_bookmarks_matches(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=['use_bookmarks = ["potato/*", "spam/*"]'],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    stack = JjClient(repo).discover_review_stack()
    run_command(
        ["jj", "bookmark", "create", "potato/feature-1", "-r", stack.revisions[0].commit_id], repo
    )
    run_command(
        ["jj", "bookmark", "create", "spam/feature-1", "-r", stack.revisions[0].commit_id], repo
    )

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple existing bookmarks match the configured bookmark patterns" in captured.err
    assert ReviewStateStore.for_repo(repo).load().changes == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.pull_requests == {}


def test_submit_preserves_cached_review_decision(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )

    assert run_main(repo, config_path, "status", change_id) == 0
    capsys.readouterr()
    assert state_store.load().changes[change_id].pr_review_decision == "approved"

    assert run_main(repo, config_path, "submit", change_id) == 0
    capsys.readouterr()

    refreshed_state = state_store.load()
    assert refreshed_state.changes[change_id].pr_review_decision == "approved"
    assert refreshed_state.changes[change_id].pr_state == "open"


def test_submit_publish_marks_existing_draft_pull_requests_ready_for_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit", "--draft") == 0
    capsys.readouterr()
    assert fake_repo.pull_requests[1].is_draft is True

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id

    exit_code = run_main(repo, config_path, "submit", "--publish", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert not fake_repo.pull_requests[1].is_draft
    assert refreshed_state.changes[change_id].pr_is_draft is False


def test_submit_checkpoints_successful_in_flight_pull_request_before_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id

    app = create_app(FakeGithubState.single_repository(fake_repo))

    class FailSpecificPullRequestClient(GithubClient):
        async def create_pull_request(
            self,
            owner,
            repo,
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
                owner,
                repo,
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
        modules=("jj_review.commands.submit.command",),
        client_type=FailSpecificPullRequestClient,
    )

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code != 0

    state = ReviewStateStore.for_repo(repo).load()
    assert state.changes.get(change_id_1) is not None
    assert state.changes[change_id_1].pr_number is not None
    change2 = state.changes.get(change_id_2)
    assert change2 is None or change2.pr_number is None
    assert len(fake_repo.pull_requests) == 1
    assert fake_repo.pull_requests[1].title == "feature 1"
    pushed_review_refs = {
        ref: target
        for ref, target in remote_refs(fake_repo.git_dir).items()
        if ref.startswith("refs/heads/review/")
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
        async def add_labels(self, owner, repo, *, issue_number, labels):
            nonlocal metadata_failure_injected
            if not metadata_failure_injected:
                metadata_failure_injected = True
                raise GithubClientError(
                    "Simulated label failure",
                    status_code=500,
                )
            await super().add_labels(
                owner,
                repo,
                issue_number=issue_number,
                labels=labels,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
        client_type=FlakyMetadataClient,
    )

    assert run_main(repo, config_path, "submit") == 1
    capsys.readouterr()

    state_after_failure = ReviewStateStore.for_repo(repo).load()
    assert len(fake_repo.pull_requests) == 1
    assert state_after_failure.changes == {}
    assert fake_repo.pull_requests[1].requested_reviewers == ["alice"]
    assert fake_repo.pull_requests[1].requested_team_reviewers == ["platform"]
    assert fake_repo.pull_requests[1].labels == []

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    state_after_rerun = ReviewStateStore.for_repo(repo).load()

    assert state_after_rerun.changes[stack.revisions[0].change_id].pr_number == 1
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
        modules=("jj_review.commands.submit.command",),
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    metadata_write_calls: list[str] = []

    class NoMetadataWritesClient(GithubClient):
        async def request_reviewers(
            self,
            owner,
            repo,
            *,
            pull_number,
            reviewers,
            team_reviewers,
        ) -> None:
            metadata_write_calls.append("reviewers")
            raise AssertionError("unchanged rerun should not request reviewers")

        async def add_labels(self, owner, repo, *, issue_number, labels) -> None:
            metadata_write_calls.append("labels")
            raise AssertionError("unchanged rerun should not add labels")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
        client_type=NoMetadataWritesClient,
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert metadata_write_calls == []


def test_submit_re_request_adds_prior_approved_and_changes_requested_reviewers(
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
        modules=("jj_review.commands.submit.command",),
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="alice",
        state="APPROVED",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="alice",
        state="DISMISSED",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="bob",
        state="CHANGES_REQUESTED",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="carol",
        state="APPROVED",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="dave",
        state="COMMENTED",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="erin",
        state="CHANGES_REQUESTED",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="erin",
        state="APPROVED",
    )

    assert run_main(repo, config_path, "submit", "--re-request") == 0
    capsys.readouterr()

    assert fake_repo.pull_requests[1].requested_reviewers == [
        "pending-reviewer",
        "bob",
        "carol",
        "erin",
    ]


def test_submit_cli_reviewers_override_configured_reviewers(
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
            'reviewers = ["config-user"]',
            'team_reviewers = ["config-team"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
    )

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--reviewers",
        "alice,bob",
        "--team-reviewers",
        "platform",
        "--reviewers",
        "carol,bob",
        "--team-reviewers",
        "infra,platform",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1" in captured.out
    assert fake_repo.pull_requests[1].requested_reviewers == ["alice", "bob", "carol"]
    assert fake_repo.pull_requests[1].requested_team_reviewers == ["platform", "infra"]


def test_submit_cli_labels_override_configured_labels(
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
            'labels = ["config-label"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
    )

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--label",
        "needs-review,backend",
        "--label",
        "triaged,backend",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1" in captured.out
    assert fake_repo.pull_requests[1].labels == [
        "needs-review",
        "backend",
        "triaged",
    ]


def test_submit_checkpoints_successful_in_flight_stack_comment_before_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    for index in range(3):
        commit_file(repo, f"feature {index + 1}", f"feature-{index + 1}.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    state_store = ReviewStateStore.for_repo(repo)
    stack = JjClient(repo).discover_review_stack()
    initial_state = state_store.load()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    issue_number_1 = initial_state.changes[change_id_1].pr_number
    issue_number_2 = initial_state.changes[change_id_2].pr_number
    issue_number_3 = initial_state.changes[change_id_3].pr_number
    if issue_number_1 is None or issue_number_2 is None or issue_number_3 is None:
        raise AssertionError("Expected pull request numbers after initial submit.")

    stale_comment_1 = _navigation_comments(fake_repo, issue_number_1)[0]
    stale_comment_2 = _navigation_comments(fake_repo, issue_number_2)[0]
    stale_comment_3 = _navigation_comments(fake_repo, issue_number_3)[0]
    stale_comment_1.body = f"{STACK_NAVIGATION_COMMENT_MARKER}\nstale bottom navigation"
    stale_comment_2.body = f"{STACK_NAVIGATION_COMMENT_MARKER}\nstale middle navigation"

    app = create_app(FakeGithubState.single_repository(fake_repo))
    updated_comment_ids: list[int] = []

    class FlakyCommentClient(GithubClient):
        async def update_issue_comment(self, owner, repo, *, comment_id, body):
            updated_comment_ids.append(comment_id)
            if comment_id == stale_comment_2.id:
                await asyncio.sleep(0.01)
                raise GithubClientError(
                    "Simulated stack navigation comment failure",
                    status_code=500,
                )
            if comment_id == stale_comment_1.id:
                await asyncio.sleep(0.03)
            return await super().update_issue_comment(
                owner,
                repo,
                comment_id=comment_id,
                body=body,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
        client_type=FlakyCommentClient,
        concurrency_limits={"jj_review.commands.submit.command": 2},
    )

    assert run_main(repo, config_path, "submit") == 1
    capsys.readouterr()

    refreshed_state = state_store.load()

    assert refreshed_state.changes[change_id_1].navigation_comment_id == stale_comment_1.id
    assert refreshed_state.changes[change_id_2].navigation_comment_id == stale_comment_2.id
    assert refreshed_state.changes[change_id_3].navigation_comment_id == (
        initial_state.changes[change_id_3].navigation_comment_id
    )
    assert stale_comment_1.id in updated_comment_ids
    assert stale_comment_2.id in updated_comment_ids
    assert stale_comment_3.id not in updated_comment_ids
    assert len(_navigation_comments(fake_repo, issue_number_1)) == 1
    assert len(issue_comments(fake_repo, issue_number_2)) == 1
    assert len(issue_comments(fake_repo, issue_number_3)) == 1


def test_submit_completes_operation_journal_after_successful_submit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code == 0
    submit_events = [
        event
        for event in read_operation_log(resolve_state_path(repo).parent)
        if event.operation == "submit"
    ]
    assert [event.event for event in submit_events] == ["begin", "completed"]


def test_submit_logs_begin_after_failed_submit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id

    app = create_app(FakeGithubState.single_repository(fake_repo))
    call_count = [0]

    class FailOnFirstPRClient(GithubClient):
        async def create_pull_request(
            self,
            owner,
            repo,
            *,
            base,
            body,
            draft=False,
            head,
            title,
        ):
            call_count[0] += 1
            if call_count[0] >= 1:
                raise GithubClientError("Simulated failure on first PR", status_code=500)
            return await super().create_pull_request(
                owner,
                repo,
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
        modules=("jj_review.commands.submit.command",),
        client_type=FailOnFirstPRClient,
    )

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code != 0
    pushed_review_refs = {
        ref: target
        for ref, target in remote_refs(fake_repo.git_dir).items()
        if ref.startswith("refs/heads/review/")
    }
    assert len(pushed_review_refs) == 2
    assert set(pushed_review_refs.values()) == {
        revision.commit_id for revision in stack.revisions
    }
    submit_events = [
        event
        for event in read_operation_log(resolve_state_path(repo).parent)
        if event.operation == "submit"
    ]
    assert [event.event for event in submit_events] == ["begin"]
    begin_data = submit_events[0].data
    assert begin_data["options"]["remote_name"] == "origin"
    assert begin_data["options"]["github_host"] == "github.test"
    assert begin_data["options"]["github_owner"] == "octo-org"
    assert begin_data["options"]["github_repo"] == "stacked-review"
    stored_ids = begin_data["resolved_scope"]["ordered_change_ids"]
    stored_commit_ids = begin_data["resolved_scope"]["ordered_commit_ids"]
    assert change_id_1 in stored_ids
    assert change_id_2 in stored_ids
    assert stored_commit_ids == [revision.commit_id for revision in stack.revisions]
