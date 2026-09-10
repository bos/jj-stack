from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

import jj_stack.commands.sync_apply as sync_apply
from jj_stack.errors import EXIT_GITHUB, EXIT_INCOMPLETE, CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    remote_refs,
    run_command,
    selected_stack,
    update_remote_ref,
    write_file,
)
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    run_main,
)

# Every case in this file counts toward the merge/recovery test limit in
# complexity-budget.toml.
pytestmark = pytest.mark.merge_recovery


def _squash_merge_pr(fake_repo, pr_number: int) -> None:
    stack_number = fake_repo.stack_number_for_pr(pr_number)
    if stack_number is not None:
        del fake_repo.github_stacks[stack_number]
    fake_repo.apply_squash_merge(fake_repo.prs[pr_number])


def _simulate_stack_partial_merge(fake_repo) -> str:
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.apply_squash_merge(fake_repo.prs[1])
    return fake_repo.rewrite_pr_onto_base(
        fake_repo.prs[2],
        base_ref="main",
    )


def _add_other_workspace(repo: Path, root: Path, change: str) -> None:
    run_command(
        [
            "jj",
            "workspace",
            "add",
            "--name",
            "other",
            "--revision",
            change,
            str(root),
        ],
        repo,
    )


def test_sync_leaves_a_partially_merged_queued_pr_alone(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack_before = selected_stack(repo)
    state_before = TrackingStore.for_repo(repo).load()
    top_pr = fake_repo.prs[2]
    top_remote_before = fake_repo.ref_target(top_pr.head_ref)
    fake_repo.apply_squash_merge(fake_repo.prs[1])
    top_pr.is_queued = True

    exit_code = run_main(repo, config_path, "sync", stack_before.head.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "Stack unchanged" in captured.out
    assert fake_repo.ref_target(top_pr.head_ref) == top_remote_before
    assert tuple(change.commit_id for change in selected_stack(repo).changes) == tuple(
        change.commit_id for change in stack_before.changes
    )
    assert TrackingStore.for_repo(repo).load() == state_before


def test_sync_dry_run_previews_rebase_and_skips_submit_preview(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    top_change_id = stack.changes[1].change_id
    top_commit_id = stack.changes[1].commit_id
    original_base_ref = fake_repo.prs[2].base_ref
    _squash_merge_pr(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", "--dry-run", top_change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would remove merged changes from the bottom" in captured.out
    assert f"jj-stack sync {top_change_id[:8]}" in captured.out
    assert JjClient(repo).resolve_commit(top_change_id).commit_id == top_commit_id
    assert fake_repo.prs[2].base_ref == original_base_ref


def test_sync_recovers_an_unrecorded_fork_after_its_parent_stack_merged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    first, _second = selected_stack(repo).changes
    fake_repo.github_stacks = {7: (1, 2)}
    run_command(["jj", "new", first.change_id], repo)
    commit_file(repo, "fork work", "fork.txt")
    fork = selected_stack(repo).head
    submit_exit = run_main(repo, config_path, "submit", "--base", first.change_id, fork.change_id)
    assert submit_exit == 0, capsys.readouterr()
    baseline = state_store.load().prs[fork.change_id].submitted_baseline.commit_id
    run_command(["jj", "describe", "-r", fork.change_id, "-m", "amended fork work"], repo)
    pushed = JjClient(repo).resolve_commit(fork.change_id).commit_id

    def fail_acknowledgement(*_args, **_kwargs):
        raise CliError("injected submit acknowledgement failure")

    with monkeypatch.context() as interrupted_submit:
        interrupted_submit.setattr(TrackingStore, "relink_pr", fail_acknowledgement)
        submit_exit = run_main(
            repo, config_path, "submit", "--base", first.change_id, fork.change_id
        )
    interrupted = capsys.readouterr()
    assert submit_exit == 1
    assert "injected submit acknowledgement failure" in interrupted.err
    assert state_store.load().prs[fork.change_id].submitted_baseline.commit_id == baseline
    assert fake_repo.prs[3].head_sha == pushed != baseline
    fake_repo.apply_merge_commit((fake_repo.prs[1], fake_repo.prs[2]))

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    jj = JjClient(repo)
    rewritten_fork = jj.resolve_commit(fork.change_id)
    assert rewritten_fork.commit_id != pushed
    assert rewritten_fork.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert jj.resolve_commit("@").parents == (rewritten_fork.commit_id,)
    state = state_store.load()
    assert set(state.prs) == {fork.change_id}
    assert state.prs[fork.change_id].submitted_baseline.commit_id == rewritten_fork.commit_id
    assert (fake_repo.prs[3].head_sha, fake_repo.prs[3].base_ref) == (
        rewritten_fork.commit_id,
        "main",
    )
    # No replacement pull request was opened for the merged changes.
    assert set(fake_repo.prs) == {1, 2, 3}
    assert fake_repo.github_stacks == {7: (1, 2)}


def test_sync_recovers_a_clean_single_pr_rebase_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    submitted = selected_stack(repo).head
    state_store = TrackingStore.for_repo(repo)
    identity = state_store.load().prs[submitted.change_id].pr_identity
    pr_branch = identity.head_ref
    fake_repo.advance_branch("main", path="upstream.txt", contents="upstream\n")
    landed_commit_id = fake_repo.apply_rebase_merge(fake_repo.prs[identity.pr_number])

    exit_code = run_main(repo, config_path, "sync", submitted.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    copies = JjClient(repo).query_commits_by_change_ids((submitted.change_id,))[
        submitted.change_id
    ]
    assert tuple(item.commit_id for item in copies) == (landed_commit_id,)
    assert copies[0].immutable
    assert JjClient(repo).resolve_commit("@").parents == (landed_commit_id,)
    assert (repo / "upstream.txt").read_text() == "upstream\n"
    assert submitted.change_id not in state_store.load().prs
    assert f"refs/heads/{pr_branch}" not in remote_refs(fake_repo.git_dir)


def test_sync_all_finds_cross_workspace_recovery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (submitted,) = selected_stack(repo).changes
    other_workspace = tmp_path / "other-workspace"
    _add_other_workspace(repo, other_workspace, submitted.change_id)
    commit_file(other_workspace, "dependent", "dependent.txt")
    run_command(["jj", "edit", "@-"], other_workspace)
    dependent = JjClient(other_workspace).resolve_commit("@")
    state_store = TrackingStore.for_repo(repo)
    pr_branch = state_store.load().prs[submitted.change_id].pr_identity.head_ref
    _squash_merge_pr(fake_repo, 1)
    landed_commit_id = read_remote_ref(fake_repo.git_dir, "main")
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert submitted.change_id not in state_store.load().prs
    assert f"refs/heads/{pr_branch}" not in remote_refs(fake_repo.git_dir)
    rewritten_dependent = JjClient(other_workspace).resolve_commit("@")
    assert rewritten_dependent.change_id == dependent.change_id
    assert rewritten_dependent.parents == (landed_commit_id,)


def test_sync_all_rebases_a_workspace_child_of_an_exact_merge_side_copy(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (submitted,) = selected_stack(repo).changes
    other_workspace = tmp_path / "other-workspace"
    _add_other_workspace(repo, other_workspace, submitted.change_id)
    commit_file(other_workspace, "dependent", "dependent.txt")
    run_command(["jj", "edit", "@-"], other_workspace)
    dependent = JjClient(other_workspace).resolve_commit("@")
    fake_repo.apply_merge_commit((fake_repo.prs[1],))
    landed_commit_id = read_remote_ref(fake_repo.git_dir, "main")
    run_command(["jj", "git", "fetch"], repo)
    run_command(["jj", "new", "trunk()"], repo)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    rewritten = JjClient(other_workspace).resolve_commit("@")
    assert rewritten.change_id == dependent.change_id
    assert rewritten.parents == (landed_commit_id,)


def test_sync_all_exact_merge_does_not_select_an_unrelated_post_trunk_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (submitted,) = selected_stack(repo).changes
    state_store = TrackingStore.for_repo(repo)
    pr_branch = state_store.load().prs[submitted.change_id].pr_identity.head_ref
    fake_repo.apply_merge_commit((fake_repo.prs[1],))
    run_command(["jj", "git", "fetch"], repo)
    run_command(["jj", "new", "trunk()"], repo)
    commit_file(repo, "unrelated", "unrelated.txt")
    unrelated = JjClient(repo).resolve_commit("@")
    unrelated_snapshot = (unrelated.commit_id, unrelated.parents)
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    unchanged = JjClient(repo).resolve_commit(unrelated.change_id)
    assert (unchanged.commit_id, unchanged.parents) == unrelated_snapshot
    assert submitted.change_id not in state_store.load().prs
    assert f"refs/heads/{pr_branch}" not in remote_refs(fake_repo.git_dir)


def test_sync_all_cleans_a_rewritten_merge_after_its_local_copy_is_gone(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (submitted,) = selected_stack(repo).changes
    state_store = TrackingStore.for_repo(repo)
    pr_branch = state_store.load().prs[submitted.change_id].pr_identity.head_ref
    _squash_merge_pr(fake_repo, 1)
    run_command(["jj", "abandon", submitted.change_id], repo)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert submitted.change_id not in state_store.load().prs
    assert f"refs/heads/{pr_branch}" not in remote_refs(fake_repo.git_dir)


def test_sync_all_explains_how_to_forget_a_deleted_workspace_blocking_removal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (submitted,) = selected_stack(repo).changes
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "independent", "independent.txt")
    independent = selected_stack(repo).head
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    other_workspace = tmp_path / "other-workspace"
    _add_other_workspace(repo, other_workspace, submitted.change_id)
    run_command(["jj", "edit", submitted.change_id], other_workspace)
    other_workspace.rename(tmp_path / "deleted-workspace")
    _squash_merge_pr(fake_repo, 1)
    _squash_merge_pr(fake_repo, 2)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert submitted.change_id[:8] in captured.err
    assert "other" in captured.err
    assert "jj no longer reports a directory" in captured.err
    workspace_argument = "'other'" if sys.platform == "win32" else "other"
    assert f"jj workspace forget -- {workspace_argument}" in captured.err
    assert "If it still exists elsewhere" in captured.err
    assert str(other_workspace) not in captured.err
    merged_change = JjClient(repo).resolve_commit(submitted.change_id)
    assert merged_change.working_copy_workspaces == ("other",)
    remaining = TrackingStore.for_repo(repo).load().prs
    assert submitted.change_id in remaining
    assert independent.change_id not in remaining


def test_sync_all_preserves_tracking_when_exact_pr_head_changed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (submitted,) = selected_stack(repo).changes
    state_store = TrackingStore.for_repo(repo)
    pr = fake_repo.prs[1]
    fake_repo.apply_merge_commit((pr,))
    fake_repo.force_push_pr_head(pr)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PR #1" in captured.err
    assert "last submitted commit" in captured.err
    assert submitted.change_id in state_store.load().prs


def test_sync_all_reports_batch_pr_failure_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=1)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    async def fail_batch_lookup(self, *, pr_numbers):
        raise GithubClientError("GitHub pull request batch lookup failed: unavailable")

    monkeypatch.setattr(GithubClient, "get_prs_by_numbers", fail_batch_lookup)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert exit_code == EXIT_GITHUB
    assert "Could not inspect pull requests" in captured.err
    assert "unavailable" in captured.err
    assert "Traceback" not in captured.err


def test_sync_converges_stack_history_and_adopts_rewritten_survivor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).changes
    remote_survivor = _simulate_stack_partial_merge(fake_repo)
    # GitHub rooted the rewritten survivor at the merge result; trunk then moved on.
    advanced_trunk = fake_repo.advance_branch(
        "main", path="landed-later.txt", contents="landed after the stack merge\n"
    )
    survivor_branch = state_store.load().prs[survivor.change_id].pr_identity.head_ref
    run_command(
        ["jj", "git", "fetch", "--remote", "origin", "--branch", survivor_branch],
        repo,
    )
    view_exit_code = run_main(repo, config_path, "view", survivor.change_id)
    inspection = capsys.readouterr()

    assert view_exit_code in {0, EXIT_INCOMPLETE}
    assert survivor.change_id[:8] in inspection.out
    assert "Traceback" not in inspection.err

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    state = state_store.load()
    assert on_trunk.change_id not in state.prs
    rewritten_survivor = JjClient(repo).resolve_commit(survivor.change_id)
    assert rewritten_survivor.parents == (fake_repo.prs[1].merge_commit_sha,)
    assert (
        read_remote_ref(fake_repo.git_dir, "main")
        == advanced_trunk
        != rewritten_survivor.parents[0]
    )
    assert JjClient(repo).resolve_commit("@").parents == (rewritten_survivor.commit_id,)
    pr_branch_temp = JjClient(repo).pr_branch_temp_artifacts()
    assert (pr_branch_temp.ref_target, pr_branch_temp.bookmark_targets) == (None, ())
    assert state.prs[survivor.change_id].submitted_baseline.commit_id == (
        rewritten_survivor.commit_id
    )
    assert fake_repo.prs[2].head_sha == rewritten_survivor.commit_id
    assert fake_repo.prs[2].base_ref == "main"
    assert fake_repo.github_stacks == {7: (1, 2)}
    on_trunk_versions = JjClient(repo).query_commits_by_change_ids((on_trunk.change_id,))[
        on_trunk.change_id
    ]
    assert on_trunk_versions == ()
    assert remote_survivor != survivor.commit_id


def test_sync_rejects_unselected_mutable_copy_after_github_rewrite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    _merged, survivor = selected_stack(repo).changes
    remote_survivor = _simulate_stack_partial_merge(fake_repo)
    survivor_branch = state_store.load().prs[survivor.change_id].pr_identity.head_ref
    run_command(
        ["jj", "git", "fetch", "--remote", "origin", "--branch", survivor_branch],
        repo,
    )
    run_command(
        ["jj", "describe", "-r", survivor.commit_id, "-m", "first survivor edit"],
        repo,
    )
    run_command(
        [
            "jj",
            "describe",
            "--at-operation",
            "@-",
            "-r",
            survivor.commit_id,
            "-m",
            "second survivor edit",
        ],
        repo,
    )
    mutable_commits = tuple(
        commit
        for commit in JjClient(repo).query_commits_by_change_ids((survivor.change_id,))[
            survivor.change_id
        ]
        if not commit.immutable
    )
    state_before = state_store.load()
    refs_before = remote_refs(fake_repo.git_dir)
    prs_before = deepcopy(fake_repo.prs)

    assert len(mutable_commits) == 2

    exit_code = run_main(repo, config_path, "sync", remote_survivor)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "more than one mutable local copy" in captured.err
    assert "jj converge -r" in captured.err
    assert state_store.load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before
    assert fake_repo.prs == prs_before


def test_sync_refuses_to_rebase_an_edited_survivor_beside_its_github_rewrite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    _merged, survivor = selected_stack(repo).changes
    _simulate_stack_partial_merge(fake_repo)
    survivor_branch = state_store.load().prs[survivor.change_id].pr_identity.head_ref
    run_command(
        ["jj", "git", "fetch", "--remote", "origin", "--branch", survivor_branch],
        repo,
    )
    run_command(["jj", "describe", "-r", survivor.commit_id, "-m", "survivor edit"], repo)
    state_before = state_store.load()
    refs_before = remote_refs(fake_repo.git_dir)
    prs_before = deepcopy(fake_repo.prs)

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "multiple visible commits" in captured.err
    assert "jj converge -r" in captured.err
    assert state_store.load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before
    assert fake_repo.prs == prs_before


def test_sync_noop_after_partial_merge_does_not_read_pr_branch_targets_or_submit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    _, survivor = selected_stack(repo).changes
    _simulate_stack_partial_merge(fake_repo)
    # Naming the merged PR selects the complete stack containing it, survivor included.
    first_exit_code = run_main(repo, config_path, "sync", "--pull-request", "1")
    first = capsys.readouterr()
    assert first_exit_code == 0, (first.out, first.err)

    survivor = selected_stack(repo).head
    pr_before = deepcopy(fake_repo.prs[2])

    async def fail_pr_branch_ref_read(*_args, **_kwargs):
        raise AssertionError("no-op sync should not read exact PR branch refs")

    monkeypatch.setattr(GithubClient, "get_branch_targets", fail_pr_branch_ref_read)
    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "No completed merges or GitHub stack rebases to sync." in captured.out
    assert "Submitted changes:" not in captured.out
    assert fake_repo.prs[2] == pr_before


def test_post_merge_sync_recovers_an_amended_survivor_after_comment_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).changes
    run_command(["jj", "edit", survivor.change_id], repo)
    write_file(repo / "local-survivor-edit.txt", "keep this edit\n")
    run_command(["jj", "new"], repo)
    load_comments = GithubClient.find_issue_comments_and_revisions
    fail_comments = True

    async def load_comments_or_fail(self, **kwargs):
        if fail_comments:
            raise GithubClientError("Comment lookup unavailable", status_code=503)
        return await load_comments(self, **kwargs)

    monkeypatch.setattr(GithubClient, "find_issue_comments_and_revisions", load_comments_or_fail)

    exit_code = run_main(repo, config_path, "merge", "--pull-request", "1")
    captured = capsys.readouterr()

    assert exit_code == EXIT_GITHUB, (captured.out, captured.err)
    error = " ".join(captured.err.split())
    assert "Continue with jj-stack sync" not in error
    assert f"jj-stack submit {survivor.change_id[:8]}" in error
    assert "jj-stack cleanup --pull-request 1" in error

    fail_comments = False
    assert run_main(repo, config_path, "submit", survivor.change_id) == 0
    assert run_main(repo, config_path, "cleanup", "--pull-request", "1") == 0
    jj = JjClient(repo)
    republished = jj.resolve_commit(survivor.change_id)
    assert republished.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert (repo / "local-survivor-edit.txt").read_text() == "keep this edit\n"
    assert read_remote_ref(fake_repo.git_dir, fake_repo.prs[2].head_ref) == republished.commit_id
    assert fake_repo.prs[2].base_ref == "main"
    assert jj.query_commits_by_change_ids((on_trunk.change_id,))[on_trunk.change_id] == ()
    assert on_trunk.change_id not in state_store.load().prs


def test_sync_preserves_a_conflict_resolution_that_restores_the_submitted_tree(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    (submitted,) = selected_stack(repo).changes
    state_store = TrackingStore.for_repo(repo)
    state_before = state_store.load()
    baseline = state_before.prs[submitted.change_id].submitted_baseline.commit_id
    _squash_merge_pr(fake_repo, 1)
    fake_repo.advance_branch(
        "main",
        path="feature-1.txt",
        contents="feature 1 changed on trunk\n",
    )
    run_command(["jj", "git", "fetch"], repo)
    run_command(["jj", "rebase", "-s", submitted.change_id, "-d", "trunk()"], repo)
    run_command(["jj", "edit", submitted.change_id], repo)
    write_file(repo / "feature-1.txt", "feature 1\n")
    run_command(["jj", "new"], repo)
    resolved = JjClient(repo).resolve_commit(submitted.change_id)

    assert not resolved.conflict
    assert not resolved.empty
    assert resolved.commit_id != baseline
    assert (
        run_command(["jj", "diff", "--from", baseline, "--to", resolved.commit_id], repo).stdout
        == ""
    )
    assert run_command(
        ["jj", "diff", "--from", resolved.parents[0], "--to", resolved.commit_id], repo
    ).stdout

    blocked = run_main(repo, config_path, "sync", submitted.change_id)
    captured = capsys.readouterr()

    assert blocked == 1
    assert "could discard local work" in captured.err
    error = " ".join(captured.err.split())
    assert f"jj rebase -s {submitted.change_id[:8]} -d 'trunk()'" in error, error
    assert f"jj diff -r {submitted.change_id[:8]}" in error, error
    assert JjClient(repo).resolve_commit(submitted.change_id).commit_id == resolved.commit_id
    assert state_store.load() == state_before
    assert (
        read_remote_ref(
            fake_repo.git_dir, state_before.prs[submitted.change_id].pr_identity.head_ref
        )
        == baseline
    )


def test_sync_removes_a_merged_change_that_a_local_rebase_emptied(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).changes
    _simulate_stack_partial_merge(fake_repo)
    # `jj rebase -d main` after fetching carries the merged change along as an empty commit and
    # leaves the whole stack above the trunk tip, where `sync --all` must still find it.
    run_command(["jj", "git", "fetch", "--remote", "origin"], repo)
    run_command(["jj", "rebase", "-b", survivor.change_id, "-d", "trunk()"], repo)
    jj = JjClient(repo)
    assert jj.resolve_commit(on_trunk.change_id).empty

    preview_exit_code = run_main(repo, config_path, "sync", "--dry-run", survivor.change_id)
    preview = capsys.readouterr()
    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    assert preview_exit_code == 0, (preview.out, preview.err)
    assert exit_code == 0, (captured.out, captured.err)
    assert jj.query_commits_by_change_ids((on_trunk.change_id,))[on_trunk.change_id] == ()
    assert jj.resolve_commit(survivor.change_id).parents == (
        read_remote_ref(fake_repo.git_dir, "main"),
    )
    assert on_trunk.change_id not in state_store.load().prs


def test_sync_rebases_a_conflicted_pr_before_stopping_its_update(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    on_trunk, submitted = selected_stack(repo).changes
    submitted_baseline = state_store.load().prs[submitted.change_id].submitted_baseline.commit_id

    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "left conflict", "conflict.txt")
    left = JjClient(repo).resolve_commit("@-")
    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "right conflict", "conflict.txt")
    right = JjClient(repo).resolve_commit("@-")
    run_command(["jj", "new", left.commit_id, right.commit_id], repo)
    conflict_source = JjClient(repo).resolve_commit("@")
    assert conflict_source.conflict
    run_command(
        [
            "jj",
            "restore",
            "--from",
            conflict_source.commit_id,
            "--into",
            submitted.change_id,
            "conflict.txt",
        ],
        repo,
    )
    run_command(["jj", "edit", submitted.change_id], repo)
    run_command(["jj", "new"], repo)
    run_command(
        ["jj", "abandon", conflict_source.commit_id, left.commit_id, right.commit_id],
        repo,
    )
    conflicted_before = JjClient(repo).resolve_commit(submitted.change_id)
    assert conflicted_before.conflict
    _squash_merge_pr(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", submitted.change_id)
    captured = capsys.readouterr()

    assert exit_code == 3
    rendered = " ".join(captured.err.split())
    assert "The local rebase is complete" in rendered
    assert f"jj-stack submit {submitted.change_id[:8]}" in rendered
    conflicted_after = JjClient(repo).resolve_commit(submitted.change_id)
    assert conflicted_after.conflict
    assert conflicted_after.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert conflicted_after.commit_id != conflicted_before.commit_id
    assert fake_repo.prs[2].head_sha == submitted_baseline
    assert on_trunk.change_id in state_store.load().prs


@pytest.mark.parametrize(
    ("drift", "reason", "repair"),
    (
        ("closed", "is closed, so jj-stack cannot update that PR", "jj-stack cleanup"),
        ("reviewer_commit", "matches neither this change", "jj-stack checkout --pull-request 2"),
    ),
)
def test_sync_stops_before_rebasing_when_a_survivor_pr_drifted(
    tmp_path: Path,
    monkeypatch,
    capsys,
    drift: str,
    reason: str,
    repair: str,
) -> None:
    """A closed or externally pushed survivor stops sync before it rewrites anything.

    Stopping first keeps a failed sync free of side effects: one rerun after the repair removes
    the merged change, rebases the survivor, and refreshes its pull request together. The
    pushed case uses ungrouped pull requests, because GitHub moves the heads of a stack's active
    members itself when it merges the stack, and sync adopts those moves after proving them.
    """

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).changes
    if drift == "reviewer_commit":
        fake_repo.github_stacks = {}
        fake_repo.apply_squash_merge(fake_repo.prs[1])
        fake_repo.advance_branch(
            fake_repo.prs[2].head_ref,
            path="feature-2.txt",
            contents="feature 2 with a suggestion\n",
            message="Apply suggestions from code review",
        )
    else:
        _simulate_stack_partial_merge(fake_repo)
    if drift == "closed":
        fake_repo.prs[2].state = "closed"
    state_before = state_store.load()

    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    unwrapped = " ".join(captured.err.split())
    assert "PR #2" in unwrapped and reason in unwrapped
    assert repair in unwrapped
    assert JjClient(repo).resolve_commit(on_trunk.change_id).commit_id == on_trunk.commit_id
    assert JjClient(repo).resolve_commit(survivor.change_id).commit_id == survivor.commit_id
    assert state_store.load() == state_before


def test_sync_retries_stack_adoption_after_survivor_submit_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    on_trunk, survivor = selected_stack(repo).changes
    baseline_before = state_store.load().prs[survivor.change_id].submitted_baseline
    remote_survivor = _simulate_stack_partial_merge(fake_repo)
    real_refresh = sync_apply.refresh_selected_prs

    async def fail_refresh(**_kwargs):
        raise CliError("injected survivor submit failure")

    monkeypatch.setattr(sync_apply, "refresh_selected_prs", fail_refresh)
    exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    failed = capsys.readouterr()

    assert exit_code == 1
    assert "injected survivor submit failure" in failed.err
    interrupted_state = state_store.load()
    assert on_trunk.change_id in interrupted_state.prs
    assert (
        interrupted_state.prs[survivor.change_id].submitted_baseline.commit_id == remote_survivor
    )
    assert remote_survivor != baseline_before.commit_id
    assert JjClient(repo).resolve_commit(survivor.change_id).commit_id == remote_survivor

    monkeypatch.setattr(sync_apply, "refresh_selected_prs", real_refresh)
    retry_exit_code = run_main(repo, config_path, "sync", survivor.change_id)
    retry = capsys.readouterr()

    assert retry_exit_code == 0, (retry.out, retry.err)
    recovered_state = state_store.load()
    assert on_trunk.change_id not in recovered_state.prs
    assert recovered_state.prs[survivor.change_id].submitted_baseline.commit_id == remote_survivor


def test_sync_all_requires_terminal_stack_merge_for_exact_stack_member(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first, second = selected_stack(repo).changes
    state_store = TrackingStore.for_repo(repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.auto_merge_reachable_heads = False
    update_remote_ref(fake_repo, branch="main", target=first.commit_id)

    selected_exit = run_main(repo, config_path, "sync", second.change_id)
    selected = capsys.readouterr()

    assert selected_exit == 1
    assert "also includes #1, outside the selected stack" in selected.err
    assert "jj-stack unstack --stack 7" in selected.err
    assert first.change_id in state_store.load().prs
    assert fake_repo.prs[1].state == "open"

    blocked_exit = run_main(repo, config_path, "sync", "--all")
    blocked = capsys.readouterr()

    assert blocked_exit == 1
    assert "PR #1 among the unmerged PRs" in " ".join(blocked.err.split())
    assert first.change_id in state_store.load().prs
    assert fake_repo.prs[1].state == "open"


def test_sync_does_not_trust_active_stack_head_drift_without_merged_history(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    _first, second = selected_stack(repo).changes
    baseline = state_store.load().prs[second.change_id].submitted_baseline
    fake_repo.github_stacks = {7: (1, 2)}
    drifted_head = fake_repo.force_push_pr_head(fake_repo.prs[2])

    exit_code = run_main(repo, config_path, "sync", second.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cannot verify a merge or a rebase" in captured.err
    assert state_store.load().prs[second.change_id].submitted_baseline == baseline
    assert fake_repo.prs[2].head_sha == drifted_head


def test_sync_restores_change_ids_after_an_exact_github_stack_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    original_changes = selected_stack(repo).changes
    original_state = state_store.load()
    commit_file(repo, "local trailing work", "local-trailing.txt")
    original = selected_stack(repo).changes
    fake_repo.github_stacks = {7: (1, 2)}
    trunk = fake_repo.advance_branch(
        "main",
        path="github-stack-rebase-trunk.txt",
        contents="new trunk contents\n",
    )
    github_heads = fake_repo.rebase_stack_onto_base(7, base_ref="main")
    changed_head = fake_repo.replace_pr_head_contents(
        fake_repo.prs[2],
        path="github-only-edit.txt",
        contents="not in the submitted stack\n",
    )

    # A dry run has to reach the same verdict; it is the command a user runs first.
    dry_exit = run_main(repo, config_path, "sync", "--dry-run", original[-1].change_id)
    dry = capsys.readouterr()
    rejected_exit = run_main(repo, config_path, "sync", original[-1].change_id)
    rejected = capsys.readouterr()

    assert dry_exit == 1, (dry.out, dry.err)
    assert "does not have the same contents" in dry.err
    assert rejected_exit == 1
    assert "does not have the same contents" in rejected.err
    assert state_store.load() == original_state
    assert tuple(
        JjClient(repo).resolve_commit(change.change_id).commit_id for change in original
    ) == tuple(change.commit_id for change in original)
    assert fake_repo.ref_target(fake_repo.prs[2].head_ref) == changed_head

    update_remote_ref(
        fake_repo,
        branch=fake_repo.prs[2].head_ref,
        target=github_heads[1],
    )
    provable_dry_exit = run_main(repo, config_path, "sync", "--dry-run", original[-1].change_id)
    provable_dry = capsys.readouterr()

    assert provable_dry_exit == 0, (provable_dry.out, provable_dry.err)
    assert tuple(
        JjClient(repo).resolve_commit(change.change_id).commit_id for change in original
    ) == tuple(change.commit_id for change in original)
    assert state_store.load() == original_state

    real_relink_prs = TrackingStore.relink_prs

    def fail_relink_prs(self, *, replacements):
        raise CliError("injected tracking update failure")

    monkeypatch.setattr(TrackingStore, "relink_prs", fail_relink_prs)
    interrupted_exit = run_main(repo, config_path, "sync", original[-1].change_id)
    interrupted = capsys.readouterr()

    assert interrupted_exit == 1
    assert "injected tracking update failure" in interrupted.err
    interrupted_rebase = tuple(
        JjClient(repo).resolve_commit(change.change_id) for change in original
    )
    assert tuple(
        fake_repo.ref_target(fake_repo.prs[index].head_ref) for index in (1, 2)
    ) == tuple(change.commit_id for change in interrupted_rebase[:2])
    assert state_store.load() == original_state

    monkeypatch.setattr(TrackingStore, "relink_prs", real_relink_prs)
    exit_code = run_main(repo, config_path, "sync", original[-1].change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    rewritten = tuple(JjClient(repo).resolve_commit(change.change_id) for change in original)
    assert tuple(change.change_id for change in rewritten) == tuple(
        change.change_id for change in original
    )
    assert rewritten[0].parents == (trunk,)
    assert rewritten[1].parents == (rewritten[0].commit_id,)
    assert rewritten[2].parents == (rewritten[1].commit_id,)
    assert tuple(
        fake_repo.ref_target(fake_repo.prs[index].head_ref) for index in (1, 2)
    ) == tuple(change.commit_id for change in rewritten[:2])
    assert tuple(
        state_store.load().prs[change.change_id].submitted_baseline.commit_id
        for change in original_changes
    ) == tuple(change.commit_id for change in rewritten[:2])
    assert tuple(change.commit_id for change in rewritten[:2]) != github_heads
    assert (repo / "local-trailing.txt").read_text() == "local trailing work\n"
    assert (repo / "github-stack-rebase-trunk.txt").read_text() == "new trunk contents\n"
    assert JjClient(repo).pr_branch_temp_artifacts().ref_target is None


def test_sync_rejects_a_submitted_unsubmitted_submitted_sandwich_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    on_trunk, submitted = stack.changes
    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "local middle", "local-middle.txt")
    local_middle = selected_stack(repo).head
    run_command(["jj", "rebase", "-r", submitted.change_id, "-d", local_middle.change_id], repo)
    submitted_before = JjClient(repo).resolve_commit(submitted.change_id).commit_id
    middle_before = JjClient(repo).resolve_commit(local_middle.change_id).commit_id
    _squash_merge_pr(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", submitted.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "appears above an unsubmitted change" in captured.err
    assert JjClient(repo).resolve_commit(submitted.change_id).commit_id == submitted_before
    assert JjClient(repo).resolve_commit(local_middle.change_id).commit_id == middle_before
    assert set(fake_repo.prs) == {1, 2}


def test_sync_explains_the_reported_rebase_ordering_stop_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    initial = selected_stack(repo)
    submitted = initial.head
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    identity = initial_state.prs[submitted.change_id].pr_identity
    submitted_commit_id = initial_state.prs[submitted.change_id].submitted_baseline.commit_id
    run_command(["jj", "new", initial.base_parent.commit_id], repo)
    commit_file(repo, "local lower", "local-lower.txt")
    lower = selected_stack(repo).head
    run_command(["jj", "rebase", "-r", submitted.commit_id, "-o", lower.commit_id], repo)
    local_submitted = JjClient(repo).resolve_commit(submitted.change_id)
    run_command(["jj", "new", local_submitted.commit_id], repo)
    landed_commit_id = fake_repo.apply_rebase_merge(fake_repo.prs[identity.pr_number])
    JjClient(repo).fetch_remote(remote="origin")

    change_exit = run_main(repo, config_path, "view", submitted.change_id)
    change_view = capsys.readouterr()
    pr_exit = run_main(
        repo,
        config_path,
        "view",
        "--pull-request",
        str(identity.pr_number),
    )
    pr_view = capsys.readouterr()

    assert change_exit == 0
    assert pr_exit == 0
    assert "local lower" in change_view.out and "feature 1" in change_view.out
    assert "local lower" in pr_view.out and "feature 1" in pr_view.out
    jj = JjClient(repo)
    dag_before = {item.commit_id: item for item in jj.query_commits("visible()")}
    state_before = state_store.load()
    refs_before = remote_refs(fake_repo.git_dir)
    prs_before = deepcopy(fake_repo.prs)
    reviews_before = deepcopy(fake_repo.pr_reviews)
    events_before = deepcopy(fake_repo.pr_events)

    exit_code = run_main(repo, config_path, "sync", submitted.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    unwrapped = " ".join(captured.err.split())
    assert f"Cannot sync submitted {submitted.change_id[:8]}" in unwrapped
    assert f"unmerged local changes are its parents: {lower.change_id[:8]}" in unwrapped
    assert "cannot decide whether those local changes belong before or after it" in unwrapped
    assert f"Submitted commit: {submitted_commit_id}" in unwrapped
    assert f"Local copy commit: {local_submitted.commit_id}" in unwrapped
    assert f"Trunk commit: {landed_commit_id}" in unwrapped
    assert f"jj log -r 'trunk() | (trunk()..{local_submitted.commit_id})'" in unwrapped
    assert "put the unmerged changes where you want them" in unwrapped
    assert "jj-stack view" in unwrapped
    assert "jj-stack sync <head-change-id>" in unwrapped
    assert "jj-stack cleanup" in unwrapped
    assert {item.commit_id: item for item in jj.query_commits("visible()")} == dag_before
    assert state_store.load() == state_before
    assert remote_refs(fake_repo.git_dir) == refs_before
    assert fake_repo.prs == prs_before
    assert fake_repo.pr_reviews == reviews_before
    assert fake_repo.pr_events == events_before


def test_sync_converges_selected_path_while_a_sibling_still_needs_the_merged_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    on_trunk, submitted = selected_stack(repo).changes
    run_command(["jj", "new", on_trunk.change_id], repo)
    commit_file(repo, "sibling work", "sibling.txt")
    sibling = selected_stack(repo).head
    jj = JjClient(repo)
    _squash_merge_pr(fake_repo, 1)

    exit_code = run_main(repo, config_path, "sync", submitted.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert "PR #1" in captured.out
    assert f"jj-stack sync {sibling.change_id[:8]}" in captured.out
    assert f"jj-stack sync {sibling.change_id}" not in captured.out
    rewritten_submitted = jj.resolve_commit(submitted.change_id)
    assert rewritten_submitted.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert fake_repo.prs[2].head_sha == rewritten_submitted.commit_id
    assert fake_repo.prs[2].base_ref == "main"
    assert jj.resolve_commit(sibling.change_id).parents == (on_trunk.commit_id,)
    assert jj.resolve_commit(on_trunk.change_id).commit_id == on_trunk.commit_id
    assert on_trunk.change_id in TrackingStore.for_repo(repo).load().prs


def test_sync_rebases_the_current_commit_of_trailing_local_work_without_creating_a_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    initial = selected_stack(repo)
    on_trunk, submitted = initial.changes
    commit_file(repo, "local trailing", "local-trailing.txt")
    trailing = selected_stack(repo).head
    state_before = TrackingStore.for_repo(repo).load()
    assert trailing.change_id not in state_before.prs
    _squash_merge_pr(fake_repo, 1)
    real_apply_pr_finishes = sync_apply.apply_pr_finishes

    async def describe_trailing_then_finish(**kwargs):
        # Another process rewrites a survivor after planning observed its commit.
        run_command(
            ["jj", "describe", "-r", trailing.change_id, "-m", "described during sync"], repo
        )
        return await real_apply_pr_finishes(**kwargs)

    monkeypatch.setattr(sync_apply, "apply_pr_finishes", describe_trailing_then_finish)

    exit_code = run_main(repo, config_path, "sync", trailing.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    jj = JjClient(repo)
    rewritten_submitted = jj.resolve_commit(submitted.change_id)
    rewritten_trailing = jj.resolve_commit(trailing.change_id)
    assert rewritten_submitted.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert rewritten_trailing.parents == (rewritten_submitted.commit_id,)
    assert rewritten_trailing.description.startswith("described during sync")
    assert jj.resolve_commit("@").parents == (rewritten_trailing.commit_id,)
    assert set(fake_repo.prs) == {1, 2}
    assert trailing.change_id not in TrackingStore.for_repo(repo).load().prs


def test_sync_requires_every_surviving_pr_before_rewriting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    submitted = stack.changes[1]
    submitted_before = submitted.commit_id
    _squash_merge_pr(fake_repo, 1)
    del fake_repo.prs[2]

    exit_code = run_main(repo, config_path, "sync", submitted.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no longer reports PR #2" in captured.err
    assert JjClient(repo).resolve_commit(submitted.change_id).commit_id == submitted_before
    assert set(fake_repo.prs) == {1}


def test_sync_all_finishes_exact_prs_after_an_external_fast_forward(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first, second = selected_stack(repo).changes
    state_store = TrackingStore.for_repo(repo)
    identities = {number: pr.head_ref for number, pr in fake_repo.prs.items()}
    assert run_main(repo, config_path, "unstack", second.change_id) == 0
    update_remote_ref(fake_repo, branch="main", target=second.commit_id)
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()

    # The top PR's old base does not contain its head. Retargeting it to trunk would return
    # 422, so sync closes the already-landed work on its existing base instead.
    assert exit_code == 1, (captured.out, captured.err)
    assert fake_repo.prs[2].state == "closed"
    assert fake_repo.prs[2].merged_at is None
    assert fake_repo.prs[2].base_ref == identities[1]
    assert fake_repo.ref_target(identities[2]) is None
    assert second.change_id not in state_store.load().prs
    # The initial cleanup observation still protects the bottom branch while the closed
    # top PR has a head. Its successful deletion makes a fresh cleanup safe on the rerun.
    assert first.change_id in state_store.load().prs

    retry = run_main(repo, config_path, "sync", "--all")
    retried = capsys.readouterr()

    assert retry == 0, (retried.out, retried.err)
    assert state_store.load().prs == {}
    assert all(fake_repo.ref_target(branch) is None for branch in identities.values())
    assert fake_repo.ref_target("main") == second.commit_id
