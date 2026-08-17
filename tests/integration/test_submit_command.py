from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

import pytest

from jj_stack.errors import EXIT_CONFLICTS, EXIT_GITHUB, EXIT_INCOMPLETE, EXIT_USAGE
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_MARKER,
    is_overview_comment,
)
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
    run_command,
    selected_stack,
    write_file,
)
from ..support.submit_property_harness import update_remote_ref
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


def _assert_stack_prs_match_dag(
    *,
    fake_repo,
    repo: Path,
    stack,
    trunk_branch: str = "main",
) -> None:
    state = TrackingStore.for_repo(repo).load()
    bookmarks_by_change: dict[str, str] = {}
    prs_by_change = {}
    for change in stack.changes:
        identity = state.pr_identities[change.change_id]
        bookmark = identity.head_ref
        pr_number = identity.pr_number
        bookmarks_by_change[change.change_id] = bookmark
        prs_by_change[change.change_id] = fake_repo.prs[pr_number]
        assert read_remote_ref(fake_repo.git_dir, bookmark) == change.commit_id

    for index, change in enumerate(stack.changes):
        pr = prs_by_change[change.change_id]
        expected_base = (
            bookmarks_by_change[stack.changes[index - 1].change_id] if index > 0 else trunk_branch
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
    assert fake_repo.prs[2].head_ref.startswith("team-prs/")
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert all(issue_comments(fake_repo, number) == [] for number in (1, 2))


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
    parent_branch = state.pr_identities[parent_base.change_id].head_ref
    child_changes = selected_stack(repo, child_head.change_id).changes[-child_size:]
    child_pr_numbers = tuple(
        state.pr_identities[change.change_id].pr_number for change in child_changes
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
            sibling_state.pr_identities[change.change_id].pr_number for change in sibling_changes
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
        state_with_child.pr_identities[change.change_id].pr_number
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
    assert f"jj rebase -r '{child_bottom_id}::{child_head_id}' -o 'trunk()'" in rendered
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
        state_after_sync.pr_identities[parent_survivor.change_id],
        state_after_sync.submitted_baselines[parent_survivor.change_id],
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
    assert (
        state_after_sync.pr_identities[parent_survivor.change_id]
        == (state_before.pr_identities[parent_survivor.change_id])
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
        state_after_child_submit.pr_identities[change.change_id]
        for change in (child_bottom, child_head)
    ) == tuple(
        state_with_child.pr_identities[change.change_id] for change in (child_bottom, child_head)
    )
    assert (
        survivor_pr.base_ref,
        survivor_pr.head_ref,
        survivor_pr.head_sha,
        read_remote_ref(fake_repo.git_dir, survivor_pr.head_ref),
        fake_repo.stack_number_for_pr(2),
        state_after_child_submit.pr_identities[parent_survivor.change_id],
        state_after_child_submit.submitted_baselines[parent_survivor.change_id],
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
    parent_identity = TrackingStore.for_repo(repo).load().pr_identities[parent.change_id]
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
        assert f"jj-stack submit --base {parent.change_id} {child.change_id}" in rendered
    elif drift == "remote":
        branch = parent_identity.head_ref
        submitted_target = state_before.submitted_baselines[parent.change_id].commit_id
        assert "no longer points to the submitted commit" in rendered
        assert f"{branch}@origin" in rendered
        assert f"immutable submitted commit ID {submitted_target}" in rendered
        assert "jj-stack left it untouched" in rendered
        assert "cannot repair it automatically" in rendered
        assert f"jj-stack submit --base {parent.change_id} {child.change_id}" in rendered
    else:
        child_id = child.change_id[:8]
        assert "Sync the parent PR first" in rendered
        assert f"jj rebase -r '{child_id}::{child_id}' -o 'trunk()'" in rendered
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
        modules=("jj_stack.commands.submit.command",),
        client_type=LoseFirstCreateResponseClient,
    )
    state_store = TrackingStore.for_repo(repo)

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    assert "jj-stack submit" in capsys.readouterr().err
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert len(state_store.load().pr_identities) == 2

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
    assert f"jj-stack sync {head_change_id}" in error
    assert f"jj-stack submit {head_change_id}" in error
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
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")
    commit_file(repo, "feature 4", "feature-4.txt")

    initial_stack = selected_stack(repo)
    old_bottom_change_id = initial_stack.changes[0].change_id
    old_top_change_id = initial_stack.changes[-1].change_id

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    run_command(["jj", "rebase", "-r", old_bottom_change_id, "-A", old_top_change_id], repo)
    reordered_stack = selected_stack(repo)

    assert run_main(repo, config_path, "submit", reordered_stack.head.change_id) == 0
    capsys.readouterr()

    refreshed_state = TrackingStore.for_repo(repo).load()
    bookmarks_by_subject = {
        change.subject: refreshed_state.pr_identities[change.change_id].head_ref
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
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
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
        modules=("jj_stack.commands.submit.command",),
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
    assert "Could not inspect GitHub repo" in capsys.readouterr().err
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
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert run_main(repo, config_path, "submit", reordered_head) == EXIT_GITHUB

    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == remote_before
    assert fake_repo.github_stacks == {}

    failure = "none"
    assert run_main(repo, config_path, "submit", reordered_head) == 0
    assert TrackingStore.for_repo(repo).load().pr_identities.keys() == (
        state_before.pr_identities.keys()
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
        change.change_id for change in initial_stack.changes if change.subject == "feature 2"
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    initial_state = TrackingStore.for_repo(repo).load()
    original_middle_pr_number = initial_state.pr_identities[original_middle_change_id].pr_number

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
        refreshed_state.pr_identities[original_middle_change_id].pr_number
        == original_middle_pr_number
    )
    pr_numbers = {
        refreshed_state.pr_identities[change.change_id].pr_number
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
    deferred_identity = submitted_state.pr_identities[deferred_change_id]
    deferred_baseline = submitted_state.submitted_baselines[deferred_change_id]
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
    assert refreshed_state.pr_identities[deferred_change_id] == deferred_identity
    assert refreshed_state.submitted_baselines[deferred_change_id] == deferred_baseline
    assert [
        event for event in fake_repo.pr_events if event.pr_number == deferred_identity.pr_number
    ] == deferred_events
    assert len(fake_repo.prs) == 4


def test_submit_shrinking_stack_to_one_pr_dissolves_grouping(
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


def test_submit_explicit_nonmaximal_prefix_does_not_truncate_github_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    bottom = selected_stack(repo).changes[0]
    state_before = TrackingStore.for_repo(repo).load()
    refs_before = remote_refs(fake_repo.git_dir)

    exit_code = run_main(
        repo,
        config_path,
        "submit",
        f'change_id("{bottom.change_id}")',
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "selected path stops before its local head" in captured.err
    assert fake_repo.github_stacks == {1: (1, 2, 3)}
    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before


def test_submit_cross_stack_move_rejects_destination_first_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "source 1", "source-1.txt")
    commit_file(repo, "source 2", "source-2.txt")
    source = selected_stack(repo)
    assert run_main(repo, config_path, "submit", source.head.change_id) == 0
    capsys.readouterr()

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "destination", "destination.txt")
    destination = selected_stack(repo)
    assert run_main(repo, config_path, "submit", destination.head.change_id) == 0
    capsys.readouterr()

    run_command(
        ["jj", "rebase", "-r", source.head.change_id, "-A", destination.changes[0].change_id],
        repo,
    )
    moved_destination = selected_stack(repo, source.head.change_id)
    state_before = TrackingStore.for_repo(repo).load()
    refs_before = remote_refs(fake_repo.git_dir)
    assert run_main(repo, config_path, "submit", moved_destination.head.change_id) == 1
    assert "other local path" in capsys.readouterr().err
    assert fake_repo.github_stacks == {1: (1, 2)}
    assert TrackingStore.for_repo(repo).load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before


def test_submit_uses_readable_pr_branch_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    stack = selected_stack(repo)

    assert run_main(repo, config_path, "submit") == 0
    state = TrackingStore.for_repo(repo).load()
    assert JjClient(repo).visible_pr_bookmark_targets() == {}

    for change, subject in zip(stack.changes, ("feature-1", "feature-2"), strict=True):
        branch = state.pr_identities[change.change_id].head_ref
        assert branch == f"jj-stack/{subject}-{change.change_id[:8]}"
        assert f"refs/heads/{branch}" in remote_refs(fake_repo.git_dir)


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
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
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
    assert empty_state.pr_identities == {}
    assert empty_state.submitted_baselines == {}
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
    assert set(state.pr_identities) == {shared.change_id, selected.change_id}
    assert committed_path.change_id not in state.pr_identities
    assert len(fake_repo.prs) == 2


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
    assert empty_state.pr_identities == {}
    assert empty_state.submitted_baselines == {}
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}
    assert fake_repo.prs == {}


def test_submit_describe_reads_pr_and_stack_bodies_from_files(
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
    assert empty_state.pr_identities == {}
    assert empty_state.submitted_baselines == {}
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
    assert empty_state.pr_identities == {}
    assert empty_state.submitted_baselines == {}
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

    exit_code = run_main(repo, config_path, "submit", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "Planned changes:" in captured.out
    assert "feature 1" in captured.out
    assert ": new PR" in captured.out
    assert fake_repo.prs == {}
    assert remote_refs(fake_repo.git_dir) == initial_remote_refs


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

    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 renamed"], repo)

    exit_code = run_main(repo, config_path, "submit", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "pushed, PR #1 updated" in captured.out
    assert "PR #1 updated" in captured.out
    assert fake_repo.prs[1].title == "feature 1"
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
    change_id, identity = next(iter(state.pr_identities.items()))
    old_commit = state.submitted_baselines[change_id].commit_id
    if rewrite:
        run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)
    if tracked:
        run_command(["jj", "bookmark", "track", f"{identity.head_ref}@origin"], repo)

    assert identity.head_ref in JjClient(repo).visible_pr_bookmark_targets()
    assert run_main(repo, config_path, "submit", change_id) == 0
    assert "divergent changes are not supported" not in capsys.readouterr().err

    submitted = TrackingStore.for_repo(repo).load().submitted_baselines[change_id].commit_id
    assert read_remote_ref(fake_repo.git_dir, identity.head_ref) == submitted
    assert (submitted != old_commit) is rewrite


def test_submit_rejects_a_conflicted_visible_pr_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = TrackingStore.for_repo(repo).load()
    change_id, identity = next(iter(state.pr_identities.items()))
    old_commit = state.submitted_baselines[change_id].commit_id
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    rewritten = selected_stack(repo, change_id).head.commit_id
    run_command(["jj", "git", "fetch", "--remote", "origin", "--branch", "*"], repo)
    run_command(["jj", "bookmark", "create", identity.head_ref, "-r", rewritten], repo)

    assert run_main(repo, config_path, "submit", change_id) != 0
    assert "divergent" in capsys.readouterr().err
    assert read_remote_ref(fake_repo.git_dir, identity.head_ref) == old_commit


def test_submit_rejects_divergence_kept_immutable_by_another_remote_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state = TrackingStore.for_repo(repo).load()
    change_id = next(iter(state.pr_identities))
    baseline = state.submitted_baselines[change_id].commit_id
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
    assert "divergent changes are not supported" in capsys.readouterr().err

    assert run_main(repo, config_path, "view", change_id) == EXIT_INCOMPLETE
    captured = capsys.readouterr()
    assert "feature rewritten" in captured.out
    assert "divergent" in captured.err


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
    assert f"Cannot claim visible bookmark {branch}" in capsys.readouterr().err
    assert set(remote_refs(fake_repo.git_dir)) == {"refs/heads/main"}


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
    initial_top_change_id = initial_stack.changes[-1].change_id
    initial_top_pr_number = (
        TrackingStore.for_repo(repo).load().pr_identities[initial_top_change_id].pr_number
    )
    assert len(_overview_comments(fake_repo, initial_top_pr_number)) == 1

    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit", "--describe-with", str(helper)) == 0
    capsys.readouterr()
    refreshed_stack = selected_stack(repo)
    new_top_change_id = refreshed_stack.changes[-1].change_id
    refreshed_state = TrackingStore.for_repo(repo).load()
    new_top_pr_number = refreshed_state.pr_identities[new_top_change_id].pr_number

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
    change_id = stack.changes[-1].change_id
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
    change_id = stack.changes[-1].change_id
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

    app = create_app(FakeGithubState.single_repo(fake_repo))

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
    first_prs = {number: pr.title for number, pr in fake_repo.prs.items()}

    exit_code = run_main(repo, config_path, "submit")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "already pushed" in captured.out
    assert "unchanged" in captured.out
    assert remote_refs(fake_repo.git_dir) == first_refs
    assert {number: pr.title for number, pr in fake_repo.prs.items()} == first_prs


def test_submit_updates_existing_remote_pr_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    identity = TrackingStore.for_repo(repo).load().pr_identities[change_id]
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
    assert read_remote_ref(fake_repo.git_dir, bookmark) == rewritten_stack.changes[-1].commit_id
    assert fake_repo.prs[pr_number].title == "feature 1 renamed"
    assert fake_repo.prs[pr_number].body == "feature 1 renamed"


def test_submit_rerun_recovers_after_lost_remote_update_response(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    identity = TrackingStore.for_repo(repo).load().pr_identities[change_id]
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
    identity = state_store.load().pr_identities[change_id]
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
    assert set(fake_repo.prs) == {pr_number}
    assert rewritten_state.pr_identities[change_id].head_ref == bookmark
    assert rewritten_state.pr_identities[change_id].pr_number == pr_number
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
    assert f"jj-stack sync {change_id}" in captured.err
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
    bookmark = initial_state.pr_identities[change_id].head_ref
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)

    del fake_repo.prs[1]

    exit_code = run_main(repo, config_path, "submit", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Saved pull request link exists" in captured.err
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
    bookmark = initial_state.pr_identities[change_id].head_ref
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
    assert "multiple pull requests" in captured.err
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
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    commit_file(repo, "feature 3", "feature-3.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = selected_stack(repo)
    middle_change_id = stack.changes[1].change_id
    top_change_id = stack.changes[2].change_id
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    middle_bookmark = initial_state.pr_identities[middle_change_id].head_ref
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
    assert "unexpected commit" in captured.err
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
    bookmark = state.pr_identities[change_id].head_ref

    assert exit_code == 0
    assert "Submitted changes:" in captured.out
    assert stack.changes[-1].subject in captured.out
    assert len(fake_repo.prs) == 1
    assert fake_repo.prs[1].base_ref == "main"
    assert read_remote_ref(fake_repo.git_dir, bookmark) == stack.changes[-1].commit_id


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


def test_submit_checkpoints_successful_in_flight_pr_before_failure(
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
        modules=("jj_stack.commands.submit.command",),
        client_type=FailSpecificPRClient,
    )

    exit_code = run_main(repo, config_path, "submit")
    capsys.readouterr()

    assert exit_code != 0

    state = TrackingStore.for_repo(repo).load()
    assert state.pr_identities[change_id_1].pr_number == 1
    assert change_id_1 in state.submitted_baselines
    assert change_id_2 not in state.pr_identities
    assert change_id_2 not in state.submitted_baselines
    assert len(fake_repo.prs) == 1 and fake_repo.github_stacks == {}
    assert fake_repo.prs[1].title == "feature 1"
    pushed_pr_branch_refs = {
        ref: target
        for ref, target in remote_refs(fake_repo.git_dir).items()
        if ref.startswith("refs/heads/jj-stack/")
    }
    assert len(pushed_pr_branch_refs) == 2
    assert set(pushed_pr_branch_refs.values()) == {change.commit_id for change in stack.changes}


def test_submit_rerun_converges_pr_metadata_after_partial_create_failure(
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
        modules=("jj_stack.commands.submit.command", "jj_stack.commands.relink"),
        client_type=FlakyMetadataClient,
    )

    assert run_main(repo, config_path, "submit") == EXIT_GITHUB
    capsys.readouterr()

    state_after_failure = TrackingStore.for_repo(repo).load()
    assert len(fake_repo.prs) == 1
    assert state_after_failure.pr_identities == {}
    assert state_after_failure.submitted_baselines == {}
    assert fake_repo.prs[1].requested_reviewers == ["alice"]
    assert fake_repo.prs[1].requested_team_reviewers == ["platform"]
    assert fake_repo.prs[1].labels == []

    stack = selected_stack(repo)
    change_id = stack.changes[0].change_id
    assert run_main(repo, config_path, "submit") == 1
    rejected = capsys.readouterr()
    assert "Adopt that PR explicitly with relink" in rejected.err

    assert run_main(repo, config_path, "relink", "1", change_id) == 0
    capsys.readouterr()
    assert run_main(repo, config_path, "submit", "--reviewers", "alice") == 0
    capsys.readouterr()

    state_after_rerun = TrackingStore.for_repo(repo).load()

    assert state_after_rerun.pr_identities[change_id].pr_number == 1
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
    app = create_app(FakeGithubState.single_repo(fake_repo))

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
        modules=("jj_stack.commands.submit.command",),
        client_type=NoMetadataWritesClient,
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert metadata_write_calls == []


def test_submit_explicit_reviewers_apply_to_unchanged_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_config(tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repo(fake_repo))

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

    pr = fake_repo.prs[1]
    assert pr.requested_reviewers == ["alice", "bob"]
    assert pr.requested_team_reviewers == ["platform"]


def test_submit_explicit_label_applies_to_unchanged_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    config_path = write_config(tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    app = create_app(FakeGithubState.single_repo(fake_repo))

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.submit.command",),
    )

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    assert run_main(repo, config_path, "submit", "--label", "needs-review") == 0
    capsys.readouterr()

    assert fake_repo.prs[1].labels == ["needs-review"]


def test_submit_re_request_observes_reviews_before_mutation_and_retries(
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
        modules=("jj_stack.commands.submit.command",),
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

    assert run_main(repo, config_path, "submit", "--re-request") == EXIT_GITHUB
    assert remote_refs(fake_repo.git_dir) == submitted_remote_refs

    assert run_main(repo, config_path, "submit", "--re-request") == 0
    capsys.readouterr()

    assert fake_repo.prs[1].requested_reviewers == [
        "pending-reviewer",
        "erin",
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
    empty_state = TrackingStore.for_repo(repo).load()
    assert empty_state.pr_identities == {}
    assert empty_state.submitted_baselines == {}
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
    capsys.readouterr()

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
