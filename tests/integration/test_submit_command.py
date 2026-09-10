from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from jj_stack.commands.submit.revision_comments import REVISION_HISTORY_COMMENT_MARKER
from jj_stack.errors import (
    EXIT_CONFLICTS,
    EXIT_GITHUB,
    EXIT_INCOMPLETE,
    EXIT_NO_STACK,
    EXIT_USAGE,
)
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore, resolve_state_path

from ..support.fake_github import (
    FakeGithubState,
    create_app,
)
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    patch_github_client_builders,
    remote_refs,
    run_command,
    selected_stack,
    update_remote_ref,
    write_fake_github_config,
    write_file,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    read_remote_ref,
    run_main,
)


def _overview_comments(fake_repo, issue_number: int):
    return [
        comment
        for comment in issue_comments(fake_repo, issue_number)
        if STACK_OVERVIEW_COMMENT_MARKER in comment.body
    ]


def _revision_history_comments(fake_repo, issue_number: int):
    return [
        comment
        for comment in issue_comments(fake_repo, issue_number)
        if REVISION_HISTORY_COMMENT_MARKER in comment.body
    ]


def _assert_stack_prs_match_dag(
    *,
    fake_repo,
    repo: Path,
    stack,
) -> None:
    state = TrackingStore.for_repo(repo).load()
    bookmarks_by_change: dict[str, str] = {}
    prs_by_change = {}
    for change in stack.changes:
        identity = state.prs[change.change_id].pr_identity
        bookmark = identity.head_ref
        pr_number = identity.pr_number
        bookmarks_by_change[change.change_id] = bookmark
        prs_by_change[change.change_id] = fake_repo.prs[pr_number]
        assert read_remote_ref(fake_repo.git_dir, bookmark) == change.commit_id

    for index, change in enumerate(stack.changes):
        pr = prs_by_change[change.change_id]
        expected_base = (
            bookmarks_by_change[stack.changes[index - 1].change_id] if index > 0 else "main"
        )
        assert pr.title == change.subject
        assert pr.state == "open"
        assert pr.merged_at is None
        assert pr.base_ref == expected_base


def test_submit_uses_configured_namespace_and_adds_stack_only_when_needed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=['branch_prefix = "team-prs"'],
    )
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert tuple(fake_repo.prs) == (1,)
    assert fake_repo.prs[1].head_ref.startswith("team-prs/")
    assert fake_repo.github_stacks == {}
    assert issue_comments(fake_repo, 1) == []

    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert tuple(fake_repo.prs) == (1, 2)
    assert JjClient(repo).visible_pr_bookmark_targets() == {}
    for number, change in enumerate(selected_stack(repo).changes, start=1):
        branch = f"team-prs/feature-{number}-{change.change_id[:8]}"
        assert fake_repo.prs[number].head_ref == branch
        assert read_remote_ref(fake_repo.git_dir, branch) == change.commit_id
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert all(issue_comments(fake_repo, number) == [] for number in (1, 2))


def test_submit_updates_a_tracked_branch_after_the_prefix_is_renamed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Saved tracking owns a PR branch; the configured prefix only names new ones."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=['branch_prefix = "renamed"'],
    )
    feature = selected_stack(repo).head
    head_ref = TrackingStore.for_repo(repo).load().prs[feature.change_id].pr_identity.head_ref
    run_command(["jj", "edit", feature.change_id], repo)
    write_file(repo / "feature-1.txt", "feature 1 amended\n")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert head_ref.startswith("jj-stack/")
    assert read_remote_ref(fake_repo.git_dir, head_ref) == selected_stack(repo).head.commit_id
    assert not any(
        ref.startswith("refs/heads/renamed/") for ref in remote_refs(fake_repo.git_dir)
    )


@pytest.mark.parametrize(("child_size", "base_index"), ((1, -1), (2, 0)))
def test_submit_explicit_base_creates_and_updates_only_the_child_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
    child_size: int,
    base_index: int,
) -> None:
    """A forked stack must not regroup or update its already-submitted parent PR."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    parent = selected_stack(repo)
    parent_base = parent.changes[base_index]
    parent_snapshot = {
        number: (pr.base_ref, pr.head_ref, pr.title) for number, pr in fake_repo.prs.items()
    }
    if parent_base != parent.head:
        run_command(["jj", "new", parent_base.change_id], repo)
    for number in range(1, child_size + 1):
        commit_file(repo, f"child {number}", f"child-{number}.txt")
    child_head = selected_stack(repo).head

    if parent_base != parent.head:
        rejected_refs = remote_refs(fake_repo.git_dir)
        rejected_stacks = dict(fake_repo.github_stacks)
        rejected_state = TrackingStore.for_repo(repo).load()
        assert (
            run_main(
                repo,
                config_path,
                "submit",
                "--base",
                parent.head.change_id,
                child_head.change_id,
            )
            == 1
        )
        rejected = capsys.readouterr()
        assert "is not an ancestor of the selected head" in rejected.err
        assert tuple(fake_repo.prs) == (1, 2)
        assert remote_refs(fake_repo.git_dir) == rejected_refs
        assert fake_repo.github_stacks == rejected_stacks
        assert TrackingStore.for_repo(repo).load() == rejected_state

    description_options: tuple[str, ...] = ()
    if child_size == 2:
        helper = tmp_path / "child-describe.py"
        write_file(
            helper,
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "print(json.dumps({'title': sys.argv[1], 'body': sys.argv[2]}))\n",
        )
        helper.chmod(0o755)
        description_options = ("--describe-with", str(helper))
    exit_code = run_main(
        repo,
        config_path,
        "submit",
        *description_options,
        "--base",
        parent_base.change_id,
        child_head.change_id,
    )
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    state = TrackingStore.for_repo(repo).load()
    parent_branch = state.prs[parent_base.change_id].pr_identity.head_ref
    child_changes = selected_stack(repo, child_head.change_id).changes[-child_size:]
    child_pr_numbers = tuple(
        state.prs[change.change_id].pr_identity.pr_number for change in child_changes
    )
    assert child_pr_numbers == tuple(range(3, 3 + child_size))
    assert fake_repo.prs[child_pr_numbers[0]].base_ref == parent_branch
    for previous, current in zip(child_pr_numbers, child_pr_numbers[1:], strict=False):
        assert fake_repo.prs[current].base_ref == fake_repo.prs[previous].head_ref
    expected_stacks = {(1, 2)}
    if child_size == 2:
        expected_stacks.add((3, 4))
        bounded_revset = f"{parent_base.commit_id}..{child_head.commit_id}"
        assert bounded_revset in _overview_comments(fake_repo, child_pr_numbers[-1])[0].body
    assert set(fake_repo.github_stacks.values()) == expected_stacks
    assert {
        number: (pr.base_ref, pr.head_ref, pr.title)
        for number, pr in fake_repo.prs.items()
        if number <= 2
    } == parent_snapshot

    if child_size == 2:
        fake_repo.update_pr_base(
            fake_repo.prs[child_pr_numbers[0]],
            base_ref="main",
        )
        dry_run_state = TrackingStore.for_repo(repo).load()
        dry_run_refs = remote_refs(fake_repo.git_dir)
        dry_run_stacks = dict(fake_repo.github_stacks)
        assert (
            run_main(
                repo,
                config_path,
                "submit",
                "--dry-run",
                "--base",
                parent_base.change_id,
                child_head.change_id,
            )
            == 0
        )
        capsys.readouterr()
        assert fake_repo.prs[child_pr_numbers[0]].base_ref == "main"
        assert TrackingStore.for_repo(repo).load() == dry_run_state
        assert remote_refs(fake_repo.git_dir) == dry_run_refs
        assert fake_repo.github_stacks == dry_run_stacks

    run_command(["jj", "edit", child_head.change_id], repo)
    write_file(repo / "child-update.txt", "updated\n")
    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--base",
            parent_base.change_id,
            child_head.change_id,
        )
        == 0
    )
    capsys.readouterr()
    assert tuple(fake_repo.prs) == tuple(range(1, 3 + child_size))
    assert set(fake_repo.github_stacks.values()) == expected_stacks
    assert fake_repo.prs[child_pr_numbers[0]].base_ref == parent_branch
    assert {
        number: (pr.base_ref, pr.head_ref, pr.title)
        for number, pr in fake_repo.prs.items()
        if number <= 2
    } == parent_snapshot

    if child_size == 2:
        existing_prs = {
            number: (pr.base_ref, pr.head_ref, pr.title) for number, pr in fake_repo.prs.items()
        }
        run_command(["jj", "new", parent_base.change_id], repo)
        commit_file(repo, "sibling 1", "sibling-1.txt")
        commit_file(repo, "sibling 2", "sibling-2.txt")
        sibling_head = selected_stack(repo).head
        assert (
            run_main(
                repo,
                config_path,
                "submit",
                "--base",
                parent_base.change_id,
                sibling_head.change_id,
            )
            == 0
        )
        capsys.readouterr()
        sibling_state = TrackingStore.for_repo(repo).load()
        sibling_changes = selected_stack(repo, sibling_head.change_id).changes[-2:]
        sibling_pr_numbers = tuple(
            sibling_state.prs[change.change_id].pr_identity.pr_number
            for change in sibling_changes
        )
        assert sibling_pr_numbers == (5, 6)
        assert fake_repo.prs[5].base_ref == parent_branch
        assert fake_repo.prs[6].base_ref == fake_repo.prs[5].head_ref
        assert set(fake_repo.github_stacks.values()) == {(1, 2), (3, 4), (5, 6)}
        assert {
            number: (pr.base_ref, pr.head_ref, pr.title)
            for number, pr in fake_repo.prs.items()
            if number <= 4
        } == existing_prs


def test_submit_landed_interior_base_requires_the_child_to_move_to_trunk(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A higher parent survivor must not become the inferred replacement child base."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    parent = selected_stack(repo)
    landed_base, parent_survivor = parent.changes
    run_command(["jj", "new", landed_base.change_id], repo)
    commit_file(repo, "child 1", "child-1.txt")
    child_bottom = selected_stack(repo).head
    commit_file(repo, "child 2", "child-2.txt")
    child_head = selected_stack(repo).head
    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--base",
            landed_base.change_id,
            child_head.change_id,
        )
        == 0
    )
    capsys.readouterr()
    state_with_child = TrackingStore.for_repo(repo).load()
    child_pr_numbers = tuple(
        state_with_child.prs[change.change_id].pr_identity.pr_number
        for change in (child_bottom, child_head)
    )
    assert child_pr_numbers == (3, 4)
    assert set(fake_repo.github_stacks.values()) == {(1, 2), (3, 4)}

    fake_repo.apply_squash_merge(fake_repo.prs[1])
    fake_repo.rewrite_pr_onto_base(fake_repo.prs[2], base_ref="main")
    survivor_pr = fake_repo.prs[2]
    survivor_before = (
        survivor_pr.base_ref,
        survivor_pr.head_ref,
        survivor_pr.head_sha,
        survivor_pr.state,
    )
    refs_before = remote_refs(fake_repo.git_dir)
    stacks_before = dict(fake_repo.github_stacks)
    state_before = TrackingStore.for_repo(repo).load()

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--base",
        landed_base.change_id,
        child_head.change_id,
    )
    captured = capsys.readouterr()
    rendered = " ".join((captured.out + captured.err).split())
    child_bottom_id = child_bottom.change_id[:8]
    child_head_id = child_head.change_id[:8]

    assert exit_code == 1
    assert "Sync the parent PR first" in rendered
    assert f"jj rebase -s '{child_bottom_id}' -o 'trunk()'" in rendered
    assert f"jj-stack submit {child_head_id}" in rendered
    assert "without --base" in rendered
    assert parent_survivor.change_id not in rendered
    assert (
        survivor_pr.base_ref,
        survivor_pr.head_ref,
        survivor_pr.head_sha,
        survivor_pr.state,
    ) == survivor_before
    assert remote_refs(fake_repo.git_dir) == refs_before
    assert fake_repo.github_stacks == stacks_before
    assert TrackingStore.for_repo(repo).load() == state_before

    assert run_main(repo, config_path, "sync", parent_survivor.change_id) == 0
    capsys.readouterr()
    state_after_sync = TrackingStore.for_repo(repo).load()
    survivor_after_sync = JjClient(repo).resolve_commit(parent_survivor.change_id)
    survivor_pr = fake_repo.prs[2]
    survivor_snapshot = (
        survivor_pr.base_ref,
        survivor_pr.head_ref,
        survivor_pr.head_sha,
        read_remote_ref(fake_repo.git_dir, survivor_pr.head_ref),
        fake_repo.stack_number_for_pr(2),
        state_after_sync.prs[parent_survivor.change_id].pr_identity,
        state_after_sync.prs[parent_survivor.change_id].submitted_baseline,
    )
    assert survivor_after_sync.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert survivor_pr.base_ref == "main"
    assert (
        survivor_pr.base_ref,
        survivor_pr.head_ref,
        survivor_pr.head_sha,
        survivor_pr.state,
    ) == survivor_before
    assert fake_repo.github_stacks == stacks_before
    assert state_after_sync.prs[parent_survivor.change_id].pr_identity == (
        state_before.prs[parent_survivor.change_id].pr_identity
    )

    run_command(
        [
            "jj",
            "rebase",
            "-r",
            f"{child_bottom.change_id}::{child_head.change_id}",
            "-o",
            "trunk()",
        ],
        repo,
    )
    assert run_main(repo, config_path, "submit", child_head.change_id) == 0
    capsys.readouterr()

    child_bottom_pr = fake_repo.prs[child_pr_numbers[0]]
    child_head_pr = fake_repo.prs[child_pr_numbers[1]]
    assert child_bottom_pr.base_ref == "main"
    assert child_head_pr.base_ref == child_bottom_pr.head_ref
    parent_stack_number = fake_repo.stack_number_for_pr(2)
    child_stack_number = fake_repo.stack_number_for_pr(3)
    assert parent_stack_number is not None
    assert child_stack_number is not None
    assert parent_stack_number == survivor_snapshot[4]
    assert fake_repo.github_stacks[parent_stack_number] == (1, 2)
    assert fake_repo.github_stacks[child_stack_number] == (3, 4)
    assert parent_stack_number != child_stack_number
    state_after_child_submit = TrackingStore.for_repo(repo).load()
    assert tuple(
        state_after_child_submit.prs[change.change_id].pr_identity
        for change in (child_bottom, child_head)
    ) == tuple(
        state_with_child.prs[change.change_id].pr_identity
        for change in (child_bottom, child_head)
    )
    assert (
        survivor_pr.base_ref,
        survivor_pr.head_ref,
        survivor_pr.head_sha,
        read_remote_ref(fake_repo.git_dir, survivor_pr.head_ref),
        fake_repo.stack_number_for_pr(2),
        state_after_child_submit.prs[parent_survivor.change_id].pr_identity,
        state_after_child_submit.prs[parent_survivor.change_id].submitted_baseline,
    ) == survivor_snapshot


@pytest.mark.parametrize("drift", ("local", "remote", "merged"))
def test_submit_explicit_base_requires_an_exact_open_parent_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
    drift: str,
) -> None:
    """A child must not be attached to a stale parent snapshot or a PR that already landed."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    parent = selected_stack(repo).head
    parent_identity = TrackingStore.for_repo(repo).load().prs[parent.change_id].pr_identity
    commit_file(repo, "child 1", "child-1.txt")
    child = selected_stack(repo).head
    if drift == "local":
        run_command(["jj", "edit", parent.change_id], repo)
        write_file(repo / "parent-update.txt", "updated\n")
        run_command(["jj", "status"], repo)
    elif drift == "remote":
        # The fake otherwise treats a temporary head-at-base state as a merged PR. Real
        # GitHub does not reliably perform that idealized transition after a direct push.
        fake_repo.auto_merge_reachable_heads = False
        update_remote_ref(
            fake_repo,
            branch=parent_identity.head_ref,
            target=read_remote_ref(fake_repo.git_dir, "main"),
        )
    else:
        fake_repo.apply_squash_merge(fake_repo.prs[1])
    remote_before = remote_refs(fake_repo.git_dir)
    stacks_before = dict(fake_repo.github_stacks)
    state_before = TrackingStore.for_repo(repo).load()

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--base",
        parent.change_id,
        child.change_id,
    )
    captured = capsys.readouterr()
    rendered = " ".join((captured.out + captured.err).split())

    assert exit_code == 1
    if drift == "local":
        assert "changed since its last submit" in rendered
        assert f"jj-stack submit --base {parent.change_id[:8]} {child.change_id[:8]}" in rendered
    elif drift == "remote":
        branch = parent_identity.head_ref
        submitted_target = state_before.prs[parent.change_id].submitted_baseline.commit_id
        assert "no longer points to the submitted commit" in rendered
        assert f"{branch}@origin" in rendered
        assert (
            f"back to commit {submitted_target}, the commit last submitted for the base"
            in rendered
        )
        assert "jj-stack left it untouched" in rendered
        assert "cannot repair it automatically" in rendered
        assert f"jj-stack submit --base {parent.change_id[:8]} {child.change_id[:8]}" in rendered
    else:
        child_id = child.change_id[:8]
        assert "Sync the parent PR first" in rendered
        assert f"jj rebase -s '{child_id}' -o 'trunk()'" in rendered
        assert f"jj-stack submit {child_id}" in rendered
        assert "without --base" in rendered
    assert tuple(fake_repo.prs) == (1,)
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert fake_repo.github_stacks == stacks_before
    assert TrackingStore.for_repo(repo).load() == state_before


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
    app = create_app(FakeGithubState.single_repo(fake_repo))

    class LoseFirstCreateResponseClient(GithubClient):
        async def create_stack(self, *, pr_numbers):
            await super().create_stack(pr_numbers=pr_numbers)
            raise GithubClientError("Simulated lost response", status_code=500)

        async def append_to_stack(self, *, stack_number, pr_numbers):
            appended.append(tuple(pr_numbers))
            if len(appended) == 1:
                fake_repo.prs[pr_numbers[0]].is_queued = True
            return await super().append_to_stack(
                stack_number=stack_number,
                pr_numbers=pr_numbers,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=LoseFirstCreateResponseClient,
    )
    state_store = TrackingStore.for_repo(repo)

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    assert "jj-stack submit" in capsys.readouterr().err
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert len(state_store.load().prs) == 2

    top_change_id = selected_stack(repo).changes[-1].change_id
    run_command(
        ["jj", "describe", "-r", top_change_id, "-m", "feature 2 renamed\n\nupdated body"],
        repo,
    )
    stack_description = tmp_path / "stack.md"
    write_file(stack_description, "GitHub stack overview\n")

    assert run_main(repo, config_path, "submit", "--describe", f"stack={stack_description}") == 0
    assert fake_repo.prs[2].title == "feature 2 renamed"
    assert fake_repo.prs[2].body == "updated body"
    assert "GitHub stack overview" in _overview_comments(fake_repo, 2)[0].body

    for number in range(3, 6):
        commit_file(repo, f"feature {number}", f"feature-{number}.txt")
    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    assert fake_repo.github_stacks == {1: (1, 2)}
    fake_repo.prs[3].is_queued = False
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
    pr = fake_repo.prs[1]
    pr.is_queued = True
    remote_before = remote_refs(fake_repo.git_dir)
    state_store = TrackingStore.for_repo(repo)
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
    assert f"jj-stack sync {head_change_id[:8]}" in error
    assert f"jj-stack submit {head_change_id[:8]}" in error
    assert "remove PR #1 from the queue" not in error
    assert tuple(fake_repo.prs) == (1,)
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert state_store.load() == state_before


def test_submit_recreates_github_stack_only_after_active_pr_grows_to_two(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.apply_squash_merge(fake_repo.prs[1])
    JjClient(repo).ensure_pr_branch_fetch_isolation(
        remote="origin",
    )
    run_command(["jj", "git", "fetch", "--remote", "origin"], repo)
    active_change_id = selected_stack(repo).head.change_id
    run_command(["jj", "rebase", "-s", active_change_id, "-d", "main"], repo)

    exit_code = run_main(repo, config_path, "submit", active_change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert fake_repo.github_stacks == {7: (1,)}
    assert fake_repo.prs[1].merged_at is not None
    assert fake_repo.prs[2].state == "open"
    assert fake_repo.prs[2].base_ref == "main"

    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit") == 0
    assert fake_repo.github_stacks == {2: (2, 3), 7: (1,)}


def test_submit_appends_to_active_suffix_after_historical_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.apply_squash_merge(fake_repo.prs[1])
    fake_repo.update_pr_base(
        fake_repo.prs[2],
        base_ref="main",
    )
    JjClient(repo).ensure_pr_branch_fetch_isolation(
        remote="origin",
    )
    run_command(["jj", "git", "fetch", "--remote", "origin"], repo)
    active_change_id = selected_stack(repo).head.change_id
    run_command(["jj", "rebase", "-s", active_change_id, "-d", "main"], repo)
    commit_file(repo, "feature 3", "feature-3.txt")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert fake_repo.github_stacks == {7: (1, 2, 3)}
    assert fake_repo.prs[1].merged_at is not None
    assert fake_repo.prs[2].base_ref == "main"
    assert fake_repo.prs[3].base_ref == fake_repo.prs[2].head_ref


def test_submit_retargets_stale_pr_bases_before_pushing_reordered_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=4)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    initial_stack = selected_stack(repo)
    old_bottom_change_id = initial_stack.changes[0].change_id
    old_top_change_id = initial_stack.changes[-1].change_id

    run_command(["jj", "rebase", "-r", old_bottom_change_id, "-A", old_top_change_id], repo)
    reordered_stack = selected_stack(repo)

    assert run_main(repo, config_path, "submit", reordered_stack.head.change_id) == 0
    assert "Dissolved GitHub stack #1, created GitHub stack #2." in capsys.readouterr().out

    refreshed_state = TrackingStore.for_repo(repo).load()
    bookmarks_by_subject = {
        change.subject: refreshed_state.prs[change.change_id].pr_identity.head_ref
        for change in reordered_stack.changes
    }
    assert all(pr.state == "open" for pr in fake_repo.prs.values())
    assert (len(fake_repo.prs), fake_repo.github_stacks) == (
        4,
        {2: (2, 3, 4, 1)},
    )
    assert fake_repo.prs[2].base_ref == "main"
    assert fake_repo.prs[3].base_ref == bookmarks_by_subject["feature 2"]
    assert fake_repo.prs[4].base_ref == bookmarks_by_subject["feature 3"]
    assert fake_repo.prs[1].base_ref == bookmarks_by_subject["feature 4"]


def test_submit_stack_preflight_failures_recover_without_persisted_phase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    app = create_app(FakeGithubState.single_repo(fake_repo))
    failure = "availability"

    class PreflightFailureClient(GithubClient):
        async def list_stacks(self):
            if failure == "availability":
                raise GithubClientError("Not Found", status_code=404)
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
        client_type=PreflightFailureClient,
    )
    state_before = TrackingStore.for_repo(repo).load()
    remote_before = remote_refs(fake_repo.git_dir)
    prs_before = {number: asdict(pr) for number, pr in fake_repo.prs.items()}
    stacks_before = dict(fake_repo.github_stacks)

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    error = capsys.readouterr().err
    assert "GitHub stacked pull requests are unavailable" in error
    assert "https://gh.io/stacksbeta" in error
    assert "repo not found" not in error
    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert {number: asdict(pr) for number, pr in fake_repo.prs.items()} == prs_before
    assert fake_repo.github_stacks == stacks_before

    failure = "membership"
    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    assert "Could not inspect GitHub stack membership" in capsys.readouterr().err
    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert fake_repo.github_stacks == stacks_before

    # Reordering the stack makes the desired membership differ from the live one, so submit
    # must unstack the resource before it can move any branch or base.
    failure = "unstack"
    original = selected_stack(repo)
    run_command(
        ["jj", "rebase", "-r", original.changes[0].change_id, "-A", original.head.change_id],
        repo,
    )
    reordered_head = selected_stack(repo).head.change_id
    state_before = TrackingStore.for_repo(repo).load()
    remote_before = remote_refs(fake_repo.git_dir)

    assert run_main(repo, config_path, "submit", "--dry-run", reordered_head) == 0
    preview = capsys.readouterr().out
    assert "dissolve GitHub stack #1" in preview
    assert "create a GitHub stack with 2 PRs" in preview
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert run_main(repo, config_path, "submit", reordered_head) == EXIT_GITHUB

    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert fake_repo.github_stacks == {}

    failure = "none"
    assert run_main(repo, config_path, "submit", reordered_head) == 0
    assert TrackingStore.for_repo(repo).load().prs.keys() == (state_before.prs.keys())
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
        change.change_id for change in initial_stack.changes if change.subject == "feature 2"
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    initial_state = TrackingStore.for_repo(repo).load()
    original_middle_pr_number = initial_state.prs[original_middle_change_id].pr_identity.pr_number

    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setenv("VISUAL", "true")
    monkeypatch.setenv("JJ_EDITOR", "true")
    run_command(
        ["jj", "split", "-r", original_middle_change_id, "feature-2a.txt"],
        repo,
    )

    split_stack = selected_stack(repo)
    assert len(split_stack.changes) == 4
    assert split_stack.changes[0].subject == "feature 1"
    assert split_stack.changes[-1].subject == "feature 3"

    assert run_main(repo, config_path, "submit", split_stack.head.change_id) == 0
    capsys.readouterr()

    refreshed_state = TrackingStore.for_repo(repo).load()
    assert (
        refreshed_state.prs[original_middle_change_id].pr_identity.pr_number
        == original_middle_pr_number
    )
    pr_numbers = {
        refreshed_state.prs[change.change_id].pr_identity.pr_number
        for change in split_stack.changes
    }
    assert len(pr_numbers) == 4
    assert all(fake_repo.prs[pr_number].state == "open" for pr_number in pr_numbers)
    assert len(fake_repo.prs) == 4


def test_submit_split_path_rebuilds_selected_github_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A GitHub stack spanning two local paths must be dissolved before either is updated."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=4)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_ids = [change.change_id for change in selected_stack(repo).changes]
    submitted_state = TrackingStore.for_repo(repo).load()
    deferred_change_id = change_ids[1]
    deferred_identity = submitted_state.prs[deferred_change_id].pr_identity
    deferred_baseline = submitted_state.prs[deferred_change_id].submitted_baseline
    deferred_pr = fake_repo.prs[deferred_identity.pr_number]
    shared_base_ref = deferred_pr.base_ref
    deferred_remote_target = read_remote_ref(fake_repo.git_dir, deferred_identity.head_ref)
    deferred_events = [
        event for event in fake_repo.pr_events if event.pr_number == deferred_identity.pr_number
    ]

    run_command(["jj", "rebase", "-s", change_ids[2], "-d", change_ids[0]], repo)
    fork_stack = selected_stack(repo, change_ids[3])
    assert [change.change_id for change in fork_stack.changes] == [
        change_ids[0],
        change_ids[2],
        change_ids[3],
    ]

    exit_code = run_main(repo, config_path, "submit", change_ids[3])
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert fake_repo.github_stacks == {2: (1, 3, 4)}
    _assert_stack_prs_match_dag(fake_repo=fake_repo, repo=repo, stack=fork_stack)

    refreshed_state = TrackingStore.for_repo(repo).load()
    assert deferred_pr.base_ref == shared_base_ref
    assert deferred_pr.head_ref == deferred_identity.head_ref
    assert deferred_pr.state == "open"
    assert deferred_pr.merged_at is None
    assert read_remote_ref(fake_repo.git_dir, deferred_identity.head_ref) == (
        deferred_remote_target
    )
    assert refreshed_state.prs[deferred_change_id].pr_identity == deferred_identity
    assert refreshed_state.prs[deferred_change_id].submitted_baseline == deferred_baseline
    assert [
        event for event in fake_repo.pr_events if event.pr_number == deferred_identity.pr_number
    ] == deferred_events
    assert len(fake_repo.prs) == 4


def test_submit_shrinking_stack_to_one_pr_dissolves_stack(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    bottom, top = stack.changes

    monkeypatch.setenv("JJ_EDITOR", "true")
    run_command(["jj", "squash", "--from", top.change_id, "--into", bottom.change_id], repo)
    survivor = selected_stack(repo, bottom.change_id)

    assert run_main(repo, config_path, "submit", survivor.head.change_id) == 0
    assert fake_repo.github_stacks == {}
    _assert_stack_prs_match_dag(fake_repo=fake_repo, repo=repo, stack=survivor)


def test_submit_selection_below_the_stack_top_does_not_truncate_github_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A described, nonempty working copy in another workspace is an ordinary stack head."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    run_command(["jj", "edit", stack.changes[2].change_id], repo)
    other_workspace = tmp_path / "other-workspace"
    run_command(
        ["jj", "workspace", "add", "-r", stack.changes[1].change_id, str(other_workspace)],
        repo,
    )
    state_before = TrackingStore.for_repo(repo).load()
    refs_before = remote_refs(fake_repo.git_dir)

    exit_code = run_main(other_workspace, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "selected changes stop below the top of the local stack" in captured.err
    assert "#3" in captured.err
    assert fake_repo.github_stacks == {1: (1, 2, 3)}
    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before


def test_submit_nonmaximal_path_dissolves_stack_around_orphan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    bottom, abandoned = selected_stack(repo).changes
    state_before = TrackingStore.for_repo(repo).load()
    abandoned_identity = state_before.prs[abandoned.change_id].pr_identity

    run_command(["jj", "abandon", abandoned.change_id], repo)
    commit_file(repo, "unsubmitted child", "unsubmitted-child.txt")

    exit_code = run_main(repo, config_path, "submit", bottom.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert fake_repo.github_stacks == {}
    assert tuple(fake_repo.prs) == (1, 2)
    assert fake_repo.prs[abandoned_identity.pr_number].state == "open"
    refreshed_state = TrackingStore.for_repo(repo).load()
    assert refreshed_state.prs[abandoned.change_id].pr_identity == abandoned_identity


def test_submit_cross_stack_move_requires_source_then_destination(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    source = selected_stack(repo)

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "destination", "destination.txt")
    destination = selected_stack(repo)
    assert run_main(repo, config_path, "submit", destination.head.change_id) == 0
    capsys.readouterr()

    run_command(
        [
            "jj",
            "rebase",
            "-r",
            source.changes[1].change_id,
            "-A",
            destination.changes[0].change_id,
        ],
        repo,
    )
    moved_destination = selected_stack(repo, source.changes[1].change_id)
    state_before = TrackingStore.for_repo(repo).load()
    fake_repo.create_pr_review(pr_number=2, reviewer_login="reviewer", state="APPROVED")
    reviews_before = deepcopy(fake_repo.pr_reviews)
    refs_before = remote_refs(fake_repo.git_dir)
    assert run_main(repo, config_path, "submit", moved_destination.head.change_id) == 1
    assert "local stack that contains the rest of GitHub stack #1" in capsys.readouterr().err
    assert fake_repo.github_stacks == {1: (1, 2, 3)}
    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before

    fake_repo.pr_events.clear()
    assert run_main(repo, config_path, "submit", source.head.change_id) == 0
    assert run_main(repo, config_path, "submit", moved_destination.head.change_id) == 0
    state_after = TrackingStore.for_repo(repo).load()
    assert {cid: record.pr_identity for cid, record in state_after.prs.items()} == {
        cid: record.pr_identity for cid, record in state_before.prs.items()
    }
    for head in (source.head.change_id, moved_destination.head.change_id):
        _assert_stack_prs_match_dag(
            fake_repo=fake_repo, repo=repo, stack=selected_stack(repo, head)
        )
    assert fake_repo.pr_reviews == reviews_before
    assert all(event.kind != "state" for event in fake_repo.pr_events)


def test_submit_draft_new_does_not_convert_published_prs_back_to_draft(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    assert not fake_repo.prs[1].is_draft

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id

    assert run_main(repo, config_path, "submit", "--draft=new", change_id) == 0
    capsys.readouterr()

    assert not fake_repo.prs[1].is_draft


def test_submit_draft_all_converts_existing_published_stack_to_draft(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    assert fake_repo.prs[1].is_draft is False
    assert fake_repo.prs[2].is_draft is False

    stack = selected_stack(repo)
    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--draft=all",
        stack.changes[-1].change_id,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "draft PR #1 updated" in captured.out
    assert "draft PR #2 updated" in captured.out
    assert fake_repo.prs[1].is_draft
    assert fake_repo.prs[2].is_draft


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
    assert "Error: Revset xporz did not resolve to a visible commit" in captured.err
    assert "jj log --no-graph" not in captured.err
    empty_state = TrackingStore.for_repo(repo).load()
    assert empty_state.prs == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.prs == {}


def test_submit_defaults_to_a_described_nonempty_working_copy(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "shared PR", "shared.txt")
    shared = selected_stack(repo).head
    commit_file(repo, "committed path", "committed.txt")
    committed_path = selected_stack(repo).head
    run_command(["jj", "new", shared.change_id], repo)
    run_command(["jj", "describe", "-m", "selected path"], repo)
    write_file(repo / "working-copy.txt", "working copy\n")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()
    selected = JjClient(repo).resolve_commit("@")
    state = TrackingStore.for_repo(repo).load()

    assert exit_code == 0, captured.err
    assert set(state.prs) == {shared.change_id, selected.change_id}
    assert committed_path.change_id not in state.prs
    assert len(fake_repo.prs) == 2


def test_submit_refuses_an_undescribed_change_below_the_selected_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """An undescribed change anywhere in the stack must not reach GitHub as a PR."""

    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    write_file(repo / "wip.txt", "wip\n")
    run_command(["jj", "status"], repo)
    undescribed = JjClient(repo).resolve_commit("@")
    run_command(["jj", "new"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == EXIT_NO_STACK
    assert undescribed.change_id[:8] in captured.err
    assert f"jj describe {undescribed.change_id[:8]}" in " ".join(captured.err.split())
    assert fake_repo.prs == {}
    assert TrackingStore.for_repo(repo).load().prs == {}


def test_submit_blocks_unresolved_conflicted_rebase_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "shared.txt")

    stack = selected_stack(repo)
    change_id = stack.changes[0].change_id

    run_command(["jj", "new", "main"], repo)
    write_file(repo / "shared.txt", "trunk 1\n")
    run_command(["jj", "commit", "-m", "trunk 1"], repo)
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    rebased_stack = selected_stack(repo, change_id)
    assert rebased_stack.changes[0].conflict is True

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == EXIT_CONFLICTS
    assert "unresolved conflicts" in captured.err
    empty_state = TrackingStore.for_repo(repo).load()
    assert empty_state.prs == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.prs == {}


def test_submit_requires_a_combined_overview_before_publishing_joined_stacks(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first_head = selected_stack(repo).head
    description = tmp_path / "stack.md"
    write_file(description, "First overview\n")
    assert run_main(repo, config_path, "submit", "--describe", f"stack={description}") == 0

    run_command(["jj", "new", "trunk()"], repo)
    commit_file(repo, "second 1", "second-1.txt")
    commit_file(repo, "second 2", "second-2.txt")
    second_bottom = selected_stack(repo).changes[0]
    write_file(description, "Second overview\n")
    assert run_main(repo, config_path, "submit", "--describe", f"stack={description}") == 0
    capsys.readouterr()

    run_command(["jj", "rebase", "-s", second_bottom.change_id, "-d", first_head.change_id], repo)
    commit_file(repo, "combined head", "combined.txt")
    refs_before = remote_refs(fake_repo.git_dir)
    stacks_before = dict(fake_repo.github_stacks)

    for options in (("--dry-run",), ()):
        assert run_main(repo, config_path, "submit", *options) == 1
        assert "--describe stack=FILE" in " ".join(capsys.readouterr().err.split())
        assert remote_refs(fake_repo.git_dir) == refs_before
        assert fake_repo.github_stacks == stacks_before
        assert set(fake_repo.prs) == {1, 2, 3, 4}
        assert "First overview" in _overview_comments(fake_repo, 2)[0].body
        assert "Second overview" in _overview_comments(fake_repo, 4)[0].body

    write_file(description, "Combined overview\n")
    assert run_main(repo, config_path, "submit", "--describe", f"stack={description}") == 0
    assert list(fake_repo.github_stacks.values()) == [(1, 2, 3, 4, 5)]
    assert "Combined overview" in _overview_comments(fake_repo, 5)[0].body
    assert _overview_comments(fake_repo, 2) == []
    assert _overview_comments(fake_repo, 4) == []


def test_submit_describe_reads_files_and_preserves_stack_overview(
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
        f"{stack.changes[0].change_id}={first_description.name}",
        "--describe",
        f"{stack.changes[1].commit_id}={second_description.name}",
        "--describe",
        f"stack={stack_description.name}",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Submitted changes:" in captured.out
    assert fake_repo.prs[1].title == "feature 1"
    assert fake_repo.prs[1].body == "First PR body\n\n- from file"
    assert fake_repo.prs[2].title == "feature 2"
    assert fake_repo.prs[2].body == "Second PR body\n\n- from file"
    assert len(_overview_comments(fake_repo, 2)) == 1
    assert STACK_OVERVIEW_COMMENT_MARKER in _overview_comments(fake_repo, 2)[0].body
    assert "Stack overview body\n\n- from file" in _overview_comments(fake_repo, 2)[0].body

    edited_overview = f"{STACK_OVERVIEW_COMMENT_MARKER}\nStack overview edited on GitHub"
    _overview_comments(fake_repo, 2)[0].body = edited_overview
    commit_file(repo, "feature 3", "feature-3.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    refreshed_stack = selected_stack(repo)
    refreshed_state = TrackingStore.for_repo(repo).load()
    top_pr_number = refreshed_state.prs[refreshed_stack.head.change_id].pr_identity.pr_number

    assert _overview_comments(fake_repo, 2) == []
    assert _overview_comments(fake_repo, top_pr_number)[0].body == edited_overview

    write_file(stack_description, "Replacement stack overview\n")

    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--describe",
            f"stack={stack_description}",
        )
        == 0
    )
    capsys.readouterr()

    replacement = _overview_comments(fake_repo, top_pr_number)[0].body
    assert "Replacement stack overview" in replacement
    assert "edited on GitHub" not in replacement

    # A one-change --base refresh of the stack head still selects a stacked PR, so the
    # overview it owns must survive.
    parent_change_id = refreshed_stack.changes[-2].change_id
    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--base",
            parent_change_id,
            refreshed_stack.head.change_id,
        )
        == 0
    )
    capsys.readouterr()

    preserved = _overview_comments(fake_repo, top_pr_number)
    assert len(preserved) == 1, "the stack overview comment was deleted"
    assert preserved[0].body == replacement


def test_submit_base_at_trunk_says_to_drop_the_flag(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """--base naming the trunk must not be reported as an unsubmitted parent change."""

    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    head_change_id = selected_stack(repo).head.change_id

    exit_code = run_main(repo, config_path, "submit", "--base", "main", head_change_id)
    captured = capsys.readouterr()
    rendered = " ".join((captured.out + captured.err).split())

    assert exit_code == 1
    assert "Base main is the trunk commit" in rendered
    assert f"Run jj-stack submit {head_change_id[:8]} without --base" in rendered
    assert "has no submitted PR" not in rendered
    assert fake_repo.prs == {}


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
    empty_state = TrackingStore.for_repo(repo).load()
    assert empty_state.prs == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.prs == {}
    assert issue_comments(fake_repo, 1) == []


def test_submit_describe_with_generates_pr_and_stack_metadata(
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
                "    changes = stack_input['changes']",
                "    payload = {",
                "        'title': 'Generated stack summary',",
                "        'body': (",
                '            f"Generated stack body for {revset}: "',
                "            f\"{changes[0]['title']} -> {changes[1]['title']} | \"",
                "            f\"{changes[0]['diffstat'].splitlines()[0]}\"",
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
    assert fake_repo.prs[1].title == f"AI {stack.changes[0].change_id[:8]}"
    assert fake_repo.prs[1].body == (f"Generated body for {stack.changes[0].change_id}")
    assert fake_repo.prs[2].title == f"AI {stack.changes[1].change_id[:8]}"
    assert fake_repo.prs[2].body == (f"Generated body for {stack.changes[1].change_id}")
    assert len(_overview_comments(fake_repo, 2)) == 1
    assert STACK_OVERVIEW_COMMENT_MARKER in _overview_comments(fake_repo, 2)[0].body
    assert "## Generated stack summary" in _overview_comments(fake_repo, 2)[0].body
    assert (
        f"Generated stack body for {stack.selected_revset}: "
        f"AI {stack.changes[0].change_id[:8]} -> AI {stack.changes[1].change_id[:8]} | "
        "feature-1.txt" in _overview_comments(fake_repo, 2)[0].body
    )

    # A visible PR bookmark leaves two visible commits for the same change, which jj
    # refuses to resolve from a bare change ID.
    run_command(
        ["jj", "describe", "-r", stack.changes[0].change_id, "-m", "feature 1 rewritten"],
        repo,
    )
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)

    assert run_main(repo, config_path, "submit", "--describe-with", str(helper)) == 0, (
        capsys.readouterr().err
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
    empty_state = TrackingStore.for_repo(repo).load()
    assert empty_state.prs == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.prs == {}
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
    JjClient(repo).ensure_pr_branch_fetch_isolation(
        remote="origin",
    )

    initial_remote_refs = remote_refs(fake_repo.git_dir)
    state_before = TrackingStore.for_repo(repo).load()

    exit_code = run_main(repo, config_path, "submit", "--dry-run", "--draft")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "create draft PR against main" in captured.out
    assert fake_repo.prs == {}
    assert fake_repo.github_stacks == {}
    assert remote_refs(fake_repo.git_dir) == initial_remote_refs
    assert TrackingStore.for_repo(repo).load() == state_before


def test_submit_dry_run_reports_update_without_mutating_remote_or_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    state_before = TrackingStore.for_repo(repo).load()
    remote_refs_before = remote_refs(fake_repo.git_dir)
    prs_before = deepcopy(fake_repo.prs)
    fake_repo.create_pr_review(pr_number=1, reviewer_login="alice", state="APPROVED")

    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        "--dry-run",
        "--draft-all",
        "--re-request",
        change_id,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "title: feature 1 renamed" in captured.out
    assert "convert to draft" in captured.out
    assert "request reviewers: alice" in captured.out
    assert fake_repo.prs == prs_before
    assert remote_refs(fake_repo.git_dir) == remote_refs_before
    assert TrackingStore.for_repo(repo).load() == state_before


@pytest.mark.parametrize(
    ("rewrite", "tracked"),
    ((False, False), (True, False), (True, True)),
)
def test_submit_accepts_a_matching_visible_pr_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
    rewrite: bool,
    tracked: bool,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = TrackingStore.for_repo(repo).load()
    change_id, identity = next(iter(state.prs.items()))
    old_commit = state.prs[change_id].submitted_baseline.commit_id
    if rewrite:
        run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)
    if tracked:
        run_command(["jj", "bookmark", "track", f"{identity.pr_identity.head_ref}@origin"], repo)

    assert identity.pr_identity.head_ref in JjClient(repo).visible_pr_bookmark_targets()
    assert run_main(repo, config_path, "submit", change_id) == 0
    assert "divergent changes are not supported" not in capsys.readouterr().err

    submitted = TrackingStore.for_repo(repo).load().prs[change_id].submitted_baseline.commit_id
    assert read_remote_ref(fake_repo.git_dir, identity.pr_identity.head_ref) == submitted
    assert (submitted != old_commit) is rewrite


def test_submit_rejects_a_conflicted_visible_pr_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = TrackingStore.for_repo(repo).load()
    change_id, identity = next(iter(state.prs.items()))
    old_commit = state.prs[change_id].submitted_baseline.commit_id
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    rewritten = selected_stack(repo, change_id).head.commit_id
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)
    run_command(
        ["jj", "bookmark", "create", identity.pr_identity.head_ref, "-r", rewritten], repo
    )

    assert run_main(repo, config_path, "submit", change_id) != 0
    assert "divergent" in capsys.readouterr().err
    assert read_remote_ref(fake_repo.git_dir, identity.pr_identity.head_ref) == old_commit


def test_submit_rejects_divergence_kept_immutable_by_another_remote_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = TrackingStore.for_repo(repo).load()
    change_id = next(iter(state.prs))
    baseline = state.prs[change_id].submitted_baseline.commit_id
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "refs/heads/other-pr-copy",
            baseline,
        ],
        repo,
    )
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)

    assert run_main(repo, config_path, "submit", change_id) == 2
    submit_output = capsys.readouterr().err
    assert "divergent changes are not supported" in submit_output
    assert "jj converge -r" in submit_output

    assert run_main(repo, config_path, "view", change_id) == EXIT_INCOMPLETE
    captured = capsys.readouterr()
    assert "feature rewritten" in captured.out
    assert "jj converge -r" in captured.out
    assert "divergent" in captured.out


def test_submit_does_not_claim_a_visible_bookmark_for_an_untracked_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    change = selected_stack(repo).head
    branch = f"jj-stack/feature-1-{change.change_id[:8]}"
    run_command(["jj", "bookmark", "create", branch, "-r", change.commit_id], repo)

    assert run_main(repo, config_path, "submit", "--dry-run", change.change_id) == 1
    assert f"Local bookmark {branch} already uses the name" in capsys.readouterr().err
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}


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


def test_submit_keeps_one_revision_history_comment_per_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = TrackingStore.for_repo(repo).load()
    change_id = next(iter(state.prs))
    first_commit = state.prs[change_id].submitted_baseline.commit_id
    assert _revision_history_comments(fake_repo, 1) == []

    run_command(["jj", "describe", "-r", change_id, "-m", "feature revision 2"], repo)
    assert run_main(repo, config_path, "submit", change_id) == 0
    capsys.readouterr()
    second_commit = (
        TrackingStore.for_repo(repo).load().prs[change_id].submitted_baseline.commit_id
    )
    first_comments = _revision_history_comments(fake_repo, 1)
    assert len(first_comments) == 1
    assert f"/compare/{first_commit}..{second_commit}" in first_comments[0].body

    run_command(["jj", "describe", "-r", change_id, "-m", "feature revision 3"], repo)
    assert run_main(repo, config_path, "submit", change_id) == 0
    capsys.readouterr()
    third_commit = TrackingStore.for_repo(repo).load().prs[change_id].submitted_baseline.commit_id
    comments = _revision_history_comments(fake_repo, 1)

    assert len(comments) == 1
    assert comments[0].id == first_comments[0].id
    assert f"/compare/{first_commit}..{second_commit}" in comments[0].body
    assert f"/compare/{second_commit}..{third_commit}" in comments[0].body


def test_submit_reports_published_prs_when_the_overview_update_needs_retrying(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nold overview",
        issue_number=2,
    )
    stack_description = tmp_path / "stack.md"
    write_file(stack_description, "New stack overview\n")
    run_command(["jj", "describe", "-r", change_id, "-m", "feature 2 revised"], repo)
    fail_update = True

    class FailingCommentUpdateClient(GithubClient):
        async def update_issue_comment(
            self,
            *,
            comment_id: int,
            body: str,
        ):
            if fail_update:
                raise GithubClientError("GitHub request failed: 404", status_code=404)
            return await super().update_issue_comment(comment_id=comment_id, body=body)

    app = create_app(FakeGithubState.single_repo(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
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
    error = " ".join(captured.err.split())
    assert "Published PR #1, PR #2" in error
    assert "Could not update stack overview comment" in error
    assert "Retry the same jj-stack submit command" in error
    assert fake_repo.prs[2].title == "feature 2 revised"
    assert (
        read_remote_ref(fake_repo.git_dir, fake_repo.prs[2].head_ref)
        == selected_stack(repo).head.commit_id
    )
    assert "old overview" in _overview_comments(fake_repo, 2)[0].body

    fail_update = False
    assert (
        run_main(
            repo,
            config_path,
            "submit",
            change_id,
            "--describe",
            f"stack={stack_description}",
        )
        == 0
    )
    assert "New stack overview" in _overview_comments(fake_repo, 2)[0].body


def test_submit_refreshes_unchanged_pr_text_and_preserves_github_edits(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    identity = TrackingStore.for_repo(repo).load().prs[change_id].pr_identity
    bookmark = identity.head_ref
    pr_number = identity.pr_number
    assert fake_repo.prs[pr_number].title == "feature 1"
    assert fake_repo.prs[pr_number].body == "feature 1"

    fake_repo.prs[pr_number].body = "Body edited on GitHub"
    run_command(["jj", "edit", change_id], repo)
    run_command(
        [
            "jj",
            "describe",
            "--ignore-immutable",
            "-r",
            change_id,
            "-m",
            "feature 1 renamed\n\nManaged body",
        ],
        repo,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = selected_stack(repo, change_id)

    assert exit_code == 0
    assert "pushed" in captured.out
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.changes[-1].commit_id
    assert fake_repo.prs[pr_number].title == "feature 1"
    assert fake_repo.prs[pr_number].body == "Body edited on GitHub"

    fake_repo.prs[pr_number].title = "Title edited on GitHub"
    explicit_body = tmp_path / "pr-body.md"
    write_file(explicit_body, "Explicit body\n")

    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--describe",
            f"{change_id}={explicit_body}",
            change_id,
        )
        == 0
    )
    capsys.readouterr()

    assert fake_repo.prs[pr_number].title == "Title edited on GitHub"
    assert fake_repo.prs[pr_number].body == "Explicit body"

    helper = tmp_path / "describe.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'title': 'Explicit title', 'body': 'Helper body'}))\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    assert run_main(repo, config_path, "submit", "--describe-with", str(helper), change_id) == 0
    capsys.readouterr()

    assert fake_repo.prs[pr_number].title == "Explicit title"
    assert fake_repo.prs[pr_number].body == "Helper body"


def test_submit_rerun_recovers_after_lost_remote_update_response(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    identity = TrackingStore.for_repo(repo).load().prs[change_id].pr_identity
    bookmark = identity.head_ref
    pr_number = identity.pr_number

    run_command(
        ["jj", "describe", "--ignore-immutable", "-r", change_id, "-m", "feature 1 renamed"],
        repo,
    )

    original_mutate = JjClient.mutate_remote_pr_branch_refs

    def mutate_then_fail(
        self,
        *,
        remote: str,
        updates,
    ) -> None:
        original_mutate(self, remote=remote, updates=updates)
        raise RuntimeError("Simulated failure after remote update")

    monkeypatch.setattr(
        "jj_stack.commands.submit.command.JjClient.mutate_remote_pr_branch_refs",
        mutate_then_fail,
    )

    with pytest.raises(RuntimeError, match="Simulated failure after remote update"):
        run_main(repo, config_path, "submit", change_id)
    capsys.readouterr()

    monkeypatch.setattr(
        "jj_stack.commands.submit.command.JjClient.mutate_remote_pr_branch_refs",
        original_mutate,
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = selected_stack(repo, change_id)

    assert exit_code == 0
    assert "updated" in captured.out
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.changes[-1].commit_id
    assert fake_repo.prs[pr_number].title == "feature 1 renamed"


@pytest.mark.merge_recovery
def test_submit_requires_relink_after_state_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    state_store = TrackingStore.for_repo(repo)
    identity = state_store.load().prs[change_id].pr_identity
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
    assert "jj-stack relink PR CHANGE" in rejected.err

    exit_code = run_main(
        repo, config_path, "relink", "--replace-remote", str(pr_number), change_id
    )
    assert exit_code == 0
    capsys.readouterr()
    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()
    rewritten_stack = selected_stack(repo, change_id)
    rewritten_state = state_store.load()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert set(fake_repo.prs) == {pr_number}
    assert rewritten_state.prs[change_id].pr_identity.head_ref == bookmark
    assert rewritten_state.prs[change_id].pr_identity.pr_number == pr_number
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.changes[-1].commit_id
    assert fake_repo.prs[pr_number].title == "feature 1 renamed"


@pytest.mark.merge_recovery
def test_submit_names_sync_when_tracked_pr_is_merged(
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
        "jj_stack.commands.submit.command.edit_prs_in_editor",
        reject_editor,
    )

    fake_repo.apply_squash_merge(fake_repo.prs[1])
    exit_code = run_main(repo, config_path, "submit", "--edit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert f"jj-stack sync {change_id[:8]}" in captured.err
    assert "relink" not in captured.err


def test_submit_fails_closed_when_cached_pr_is_missing_on_github(
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
    bookmark = initial_state.prs[change_id].pr_identity.head_ref
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)

    del fake_repo.prs[1]

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub no longer reports PR #1" in captured.err
    assert "view" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.prs == {}


def test_submit_fails_closed_when_github_reports_multiple_prs(
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
    bookmark = initial_state.prs[change_id].pr_identity.head_ref
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)
    fake_repo.create_pr(
        base_ref="main",
        body="duplicate",
        head_ref=bookmark,
        title="feature 1 duplicate",
    )

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "also has open" in captured.err
    assert "view" in captured.err
    assert "relink" in captured.err
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert set(fake_repo.prs) == {1, 2}


def test_submit_fails_closed_when_saved_remote_branch_drifted_externally(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    middle_change_id = stack.changes[1].change_id
    top_change_id = stack.changes[2].change_id
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    middle_bookmark = initial_state.prs[middle_change_id].pr_identity.head_ref
    top_target = initial_state.prs[top_change_id].submitted_baseline.commit_id

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
    prs_before = {
        number: (
            pr.base_ref,
            pr.head_ref,
            pr.state,
            pr.merged_at,
            pr.title,
            pr.body,
        )
        for number, pr in fake_repo.prs.items()
    }
    fake_repo.pr_events.clear()

    exit_code = run_main(repo, config_path, "submit", middle_change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "matches neither this change" in captured.err
    assert state_store.load() == initial_state
    assert remote_refs(fake_repo.git_dir) == drifted_refs
    assert {
        number: (
            pr.base_ref,
            pr.head_ref,
            pr.state,
            pr.merged_at,
            pr.title,
            pr.body,
        )
        for number, pr in fake_repo.prs.items()
    } == prs_before
    assert fake_repo.pr_events == []


def test_submit_accepts_stack_forked_from_trunk_ancestor(
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

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()
    state = TrackingStore.for_repo(repo).load()
    change_id = stack.changes[-1].change_id
    bookmark = state.prs[change_id].pr_identity.head_ref

    assert exit_code == 0
    assert "Submitted changes:" in captured.out
    assert "Top of stack: PR #1" in captured.out
    assert "https://github.test/octo-org/stacked-prs/pull/1" not in captured.out
    assert stack.changes[-1].subject in captured.out
    assert len(fake_repo.prs) == 1
    assert fake_repo.prs[1].base_ref == "main"
    assert read_remote_ref(fake_repo.git_dir, bookmark) == stack.changes[-1].commit_id


def test_submit_bases_on_the_default_branch_when_local_trunk_is_behind_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A stale local trunk is not a misconfigured trunk, even with another branch at it."""

    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    stale_trunk = read_remote_ref(fake_repo.git_dir, "main")
    update_remote_ref(fake_repo, branch="release", target=stale_trunk)
    fake_repo.advance_branch("main", path="upstream.txt", contents="upstream\n")

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert fake_repo.prs[1].base_ref == "main"


def test_submit_open_marks_existing_draft_prs_ready_for_review(
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
    change_id = stack.changes[-1].change_id

    assert "draft PR #1" in draft_output
    assert fake_repo.prs[1].is_draft is True

    exit_code = run_main(repo, config_path, "submit", "--open", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1 updated" in captured.out
    assert not fake_repo.prs[1].is_draft


def test_submit_retry_keeps_a_pr_created_while_another_request_failed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    stack = selected_stack(repo)
    change_id_1 = stack.changes[0].change_id
    change_id_2 = stack.changes[1].change_id

    app = create_app(FakeGithubState.single_repo(fake_repo))

    class FailSpecificPRClient(GithubClient):
        async def create_pr(
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
            return await super().create_pr(
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
        client_type=FailSpecificPRClient,
    )

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code != 0

    state = TrackingStore.for_repo(repo).load()
    assert state.prs[change_id_1].pr_identity.pr_number == 1
    assert change_id_1 in state.prs
    assert change_id_2 not in state.prs
    assert len(fake_repo.prs) == 1 and fake_repo.github_stacks == {}
    assert fake_repo.prs[1].title == "feature 1"
    pushed_pr_branch_refs = {
        ref: target
        for ref, target in remote_refs(fake_repo.git_dir).items()
        if ref.startswith("refs/heads/jj-stack/")
    }
    assert len(pushed_pr_branch_refs) == 2
    assert set(pushed_pr_branch_refs.values()) == {change.commit_id for change in stack.changes}

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
    )
    assert run_main(repo, config_path, "submit") == 0
    retried = capsys.readouterr()
    assert "relink" not in retried.out + retried.err
    assert TrackingStore.for_repo(repo).load().prs[change_id_1] == state.prs[change_id_1]
    assert len(fake_repo.prs) == 2
    assert fake_repo.github_stacks == {1: (1, 2)}
    _assert_stack_prs_match_dag(fake_repo=fake_repo, repo=repo, stack=selected_stack(repo))


def test_submit_rerun_converges_pr_metadata_after_partial_create_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_fake_github_config(
        tmp_path,
        extra_lines=[
            'labels = ["needs-review"]',
            'reviewers = ["alice"]',
            'team_reviewers = ["platform"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")

    app = create_app(FakeGithubState.single_repo(fake_repo))
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
        client_type=FlakyMetadataClient,
    )

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    capsys.readouterr()

    stack = selected_stack(repo)
    change_id = stack.changes[0].change_id
    state_after_failure = TrackingStore.for_repo(repo).load()
    assert len(fake_repo.prs) == 1
    # GitHub acknowledged the pull request, so its link must already be saved. An
    # untracked PR of submit's own making would need an explicit relink to repair.
    assert state_after_failure.prs[change_id].pr_identity.pr_number == 1
    assert change_id in state_after_failure.prs
    assert fake_repo.prs[1].requested_reviewers == ["alice"]
    assert fake_repo.prs[1].requested_team_reviewers == ["platform"]
    assert fake_repo.prs[1].labels == []

    assert run_main(repo, config_path, "submit", "--reviewers", "alice") == 0
    retried = capsys.readouterr()
    assert "relink" not in retried.out + retried.err

    state_after_rerun = TrackingStore.for_repo(repo).load()

    assert state_after_rerun.prs[change_id].pr_identity.pr_number == 1
    assert fake_repo.prs[1].requested_reviewers == ["alice"]
    assert fake_repo.prs[1].requested_team_reviewers == ["platform"]
    assert fake_repo.prs[1].labels == ["needs-review"]


def test_submit_unchanged_rerun_skips_pr_metadata_writes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_fake_github_config(
        tmp_path,
        extra_lines=[
            'labels = ["needs-review"]',
            'reviewers = ["alice"]',
            'team_reviewers = ["platform"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repo(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    metadata_write_calls: list[str] = []
    refs_before = remote_refs(fake_repo.git_dir)
    prs_before = {number: asdict(pr) for number, pr in fake_repo.prs.items()}

    class NoMetadataWritesClient(GithubClient):
        async def request_reviewers(
            self,
            *,
            pr_number,
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
        client_type=NoMetadataWritesClient,
    )

    assert run_main(repo, config_path, "submit") == 0
    output = capsys.readouterr().out
    assert "already pushed" in output
    assert "unchanged" in output
    assert remote_refs(fake_repo.git_dir) == refs_before
    assert {number: asdict(pr) for number, pr in fake_repo.prs.items()} == prs_before

    # An explicitly empty reviewer override is not a request to write configured metadata.
    assert run_main(repo, config_path, "submit", "--reviewers", "") == 0
    capsys.readouterr()

    assert metadata_write_calls == []


def test_submit_explicit_metadata_applies_to_an_unchanged_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    assert (
        run_main(
            repo,
            config_path,
            "submit",
            "--reviewers",
            "alice,bob",
            "--team-reviewers",
            "platform",
            "--label",
            "needs-review",
        )
        == 0
    )
    capsys.readouterr()
    assert run_main(repo, config_path, "submit", "--label", "needs-docs") == 0
    capsys.readouterr()

    pr = fake_repo.prs[1]
    assert pr.requested_reviewers == ["alice", "bob"]
    assert pr.requested_team_reviewers == ["platform"]
    assert pr.labels == ["needs-review", "needs-docs"]


def test_submit_re_request_observes_reviews_before_mutation_and_retries(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_fake_github_config(
        tmp_path,
        extra_lines=[
            'reviewers = ["pending-reviewer"]',
        ],
    )
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repo(fake_repo))
    fail_review_load = [True]

    class FailingReviewLoadClient(GithubClient):
        async def list_pr_reviews(self, *, pr_number):
            if fail_review_load and fail_review_load.pop():
                raise GithubClientError("Simulated review load failure", status_code=500)
            return await super().list_pr_reviews(pr_number=pr_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=FailingReviewLoadClient,
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    submitted_remote_refs = remote_refs(fake_repo.git_dir)

    fake_repo.create_pr_review(
        pr_number=1,
        reviewer_login="alice",
        state="APPROVED",
    )
    for reviewer, state in (
        ("alice", "DISMISSED"),
        ("erin", "CHANGES_REQUESTED"),
        ("erin", "APPROVED"),
        ("dave", "COMMENTED"),
    ):
        fake_repo.create_pr_review(pr_number=1, reviewer_login=reviewer, state=state)
    commit_file(repo, "feature 2", "feature-2.txt")
    editor_command = _write_edit_editor(
        tmp_path,
        "edit-feature-title.py",
        [
            "from pathlib import Path",
            "import sys",
            "",
            "path = Path(sys.argv[-1])",
            "text = path.read_text(encoding='utf-8')",
            "path.write_text(",
            "    text.replace('feature 2', 'feature 2 [edited]'),",
            "    encoding='utf-8',",
            ")",
        ],
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", editor_command)

    assert run_main(repo, config_path, "submit", "--re-request", "--edit") == EXIT_GITHUB
    failed = capsys.readouterr()
    assert remote_refs(fake_repo.git_dir) == submitted_remote_refs
    match = re.search(r"Editor file: (\S+)", failed.out)
    assert match is not None
    edit_path = Path(match.group(1))
    assert edit_path.is_file()
    assert "feature 2 [edited]" in edit_path.read_text(encoding="utf-8")

    monkeypatch.setenv("EDITOR", _write_edit_editor(tmp_path, "leave-edit.py", ["pass"]))
    exit_code = run_main(
        repo, config_path, "submit", "--re-request", "--resume-edit", str(edit_path)
    )
    assert exit_code == 0
    capsys.readouterr()

    assert fake_repo.prs[1].requested_reviewers == [
        "pending-reviewer",
        "erin",
    ]
    assert fake_repo.prs[2].title == "feature 2 [edited]"
    assert edit_path.is_file()
    edit_path.unlink()


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
    assert "jj-stack submit" in captured.err
    saved_edit = re.search(r"--resume-edit (\S+\.md)", captured.err)
    assert saved_edit is not None
    Path(saved_edit.group(1)).unlink()
    empty_state = TrackingStore.for_repo(repo).load()
    assert empty_state.prs == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.prs == {}


def test_submit_edit_sets_each_pr_draft_state(
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
    submitted = capsys.readouterr()
    recovery_copy = re.search(r"Editor file: (\S+)", submitted.out)
    assert recovery_copy is not None
    assert not Path(recovery_copy.group(1)).exists()

    assert fake_repo.prs[1].is_draft
    assert not fake_repo.prs[2].is_draft

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

    assert not fake_repo.prs[1].is_draft
    assert fake_repo.prs[2].is_draft
