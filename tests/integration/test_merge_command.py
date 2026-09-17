from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.errors import EXIT_GITHUB, CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.jj.client import JjClient
from jj_stack.state.operation_lock import try_acquire_operation_lock
from jj_stack.state.store import TrackingStore

from ..support.fake_github import (
    FakeGithubRepo,
    FakeGithubState,
    _complete_stack_merge,
    create_app,
)
from ..support.integration_helpers import (
    commit_file,
    delete_remote_ref,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    patch_github_client_builders,
    remote_refs,
    run_command,
    selected_stack,
    sign_commit,
    update_remote_ref,
)
from ..support.output_assertions import assert_output_contains
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    read_remote_ref,
    run_main,
)

# Every case in this file counts toward the merge/recovery test limit in
# complexity-budget.toml.
pytestmark = pytest.mark.merge_recovery


def test_merge_no_wait_resumes_a_pending_or_queued_request_and_syncs_after_waiting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.merge_queue_enabled = True
    fake_repo.allow_rebase_merge = True
    stack = selected_stack(repo)
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    selector = ("--pull-request", "1")
    request = (1, None, "merge_queue", stack.changes[0].commit_id)

    exit_code = run_main(repo, config_path, "merge", *selector, "--method", "rebase", "--no-wait")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert fake_repo.stack_merge_requests == [request]
    assert not fake_repo.merge_queue
    assert all(pr.state == "open" for pr in fake_repo.prs.values())
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert "ignoring --method" in captured.err
    assert "Merge requested" in captured.out
    assert "jj-stack sync" in captured.out

    # GitHub still reports the request pending, with the queue's own merge method; a rerun
    # recognizes it as the same request rather than another one.
    assert run_main(repo, config_path, "merge", *selector, "--no-wait") == 0
    pending = capsys.readouterr()
    assert "Merge requested" in pending.out and "already pending" not in pending.out
    assert fake_repo.stack_merge_requests == [request]

    # GitHub finishes the asynchronous request: the PR now sits in the queue.
    _complete_stack_merge(fake_repo, fake_repo.stack_merge_operations[1])
    assert run_main(repo, config_path, "merge", *selector, "--no-wait") == 0
    queued = capsys.readouterr()
    assert "In merge queue" in queued.out
    assert "jj-stack sync" in queued.out
    assert fake_repo.stack_merge_requests == [request]

    # While GitHub works through its queue, other jj-stack commands can still run: each queue
    # observation finds the repo's operation lock free. The local update afterwards holds it.
    monkeypatch.setattr("jj_stack.commands.merge.wait._QUEUE_POLL_INTERVAL_SECONDS", 0)
    state_dir = TrackingStore.for_repo(repo).path.parent

    def lock_is_free() -> bool:
        probe = try_acquire_operation_lock(state_dir, command="probe")
        if probe is not None:
            probe.release()
        return probe is not None

    free_during_queue_waits: list[bool] = []
    free_during_local_rewrites: list[bool] = []
    advance_merge_queue = FakeGithubRepo.advance_merge_queue

    def observe_then_advance(fake: FakeGithubRepo) -> None:
        free_during_queue_waits.append(lock_is_free())
        advance_merge_queue(fake)

    monkeypatch.setattr(FakeGithubRepo, "advance_merge_queue", observe_then_advance)
    for name in ("abandon_commits", "prepare_rebase_changes", "rebase_changes"):
        rewrite = getattr(JjClient, name)

        def observe_then_rewrite(client, *args, rewrite=rewrite, **kwargs):
            free_during_local_rewrites.append(lock_is_free())
            return rewrite(client, *args, **kwargs)

        monkeypatch.setattr(JjClient, name, observe_then_rewrite)
    assert run_main(repo, config_path, "merge", *selector) == 0
    completed = capsys.readouterr()
    assert "Merge completed" in completed.out
    assert "PR #1" in completed.err
    assert free_during_queue_waits and all(free_during_queue_waits)
    assert free_during_local_rewrites and not any(free_during_local_rewrites)
    assert fake_repo.stack_merge_requests == [request]
    assert set(TrackingStore.for_repo(repo).load().prs) == {stack.head.change_id}
    remaining = selected_stack(repo)
    assert remaining.head.change_id == stack.head.change_id
    assert remaining.head.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert fake_repo.prs[2].base_ref == "main"
    assert fake_repo.ref_target(fake_repo.prs[2].head_ref) == remaining.head.commit_id


def test_merge_reports_a_queue_ejection_and_the_documented_recovery_completes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.merge_queue_enabled = True
    monkeypatch.setattr("jj_stack.commands.merge.wait._QUEUE_POLL_INTERVAL_SECONDS", 0)
    state_store = TrackingStore.for_repo(repo)
    bottom, top = selected_stack(repo).changes
    head = top.change_id

    # The bottom PR's merge group fails: nothing merges and the next step is the same merge.
    fake_repo.queue_failures = {1}
    assert run_main(repo, config_path, "merge") == 1
    ejected = capsys.readouterr()
    assert "removed PR #1 from the merge queue: failed checks" in ejected.err
    assert f"jj-stack merge {head[:8]}" in ejected.err and "sync" not in ejected.err
    test_commit = fake_repo.queue_events[1].before_commit
    assert test_commit is not None and f"/commit/{test_commit}/checks" in ejected.err
    assert not fake_repo.merge_queue
    assert all(pr.state == "open" for pr in fake_repo.prs.values())
    assert state_store.load().prs.keys() == {bottom.change_id, top.change_id}

    # The bottom PR lands but the top one is ejected: GitHub leaves it at its submitted commit
    # on the merged PR's branch, and the next step is sync, then the remaining merge.
    fake_repo.queue_failures = {2}
    assert run_main(repo, config_path, "merge") == 1
    partial = capsys.readouterr()
    assert "removed PR #2 from the merge queue: failed checks" in partial.err
    assert f"PR #1 already merged. Run jj-stack sync {head[:8]}" in partial.err
    assert fake_repo.prs[1].merged_at is not None
    assert fake_repo.prs[2].state == "open" and not fake_repo.is_queued(2)
    assert fake_repo.ref_target(fake_repo.prs[2].head_ref) == top.commit_id
    assert fake_repo.prs[2].base_ref == fake_repo.prs[1].head_ref
    assert selected_stack(repo).changes == (bottom, top)

    assert run_main(repo, config_path, "sync", head) == 0
    capsys.readouterr()
    rebased = JjClient(repo).resolve_commit(top.change_id)
    assert rebased.parents == (read_remote_ref(fake_repo.git_dir, "main"),)
    assert set(state_store.load().prs) == {top.change_id}
    assert state_store.load().prs[top.change_id].submitted_baseline.commit_id == (
        rebased.commit_id
    )
    assert fake_repo.ref_target(fake_repo.prs[2].head_ref) == rebased.commit_id
    assert fake_repo.prs[2].base_ref == "main"

    fake_repo.queue_failures = set()
    assert run_main(repo, config_path, "merge") == 0
    completed = capsys.readouterr()
    assert "Merge completed" in completed.out
    assert fake_repo.prs[2].merged_at is not None
    assert state_store.load().prs == {}
    assert JjClient(repo).resolve_commit("@").parents == (
        read_remote_ref(fake_repo.git_dir, "main"),
    )


def test_signed_changes_require_a_method_even_when_they_are_not_being_merged_yet(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    fake_repo.allow_rebase_merge = True
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    sign_commit(repo, "@-")
    assert run_main(repo, config_path, "submit") == 0
    stack = selected_stack(repo)
    assert not stack.changes[0].signed
    assert stack.head.signed
    state_store = TrackingStore.for_repo(repo)
    state_before = state_store.load()
    refs_before = remote_refs(fake_repo.git_dir)
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "merge", "--pull-request", "1")
    captured = capsys.readouterr()

    assert exit_code == 1
    error = " ".join(captured.err.split())
    assert "signed commits" in error
    assert stack.head.change_id[:8] in error
    assert "--method" in error
    assert "jj-stack.merge_method" in error
    assert fake_repo.stack_merge_requests == []
    assert all(pr.state == "open" for pr in fake_repo.prs.values())
    assert fake_repo.prs[2].base_ref == fake_repo.prs[1].head_ref
    assert remote_refs(fake_repo.git_dir) == refs_before
    assert state_store.load() == state_before
    assert selected_stack(repo) == stack

    assert run_main(repo, config_path, "merge", "--dry-run", "--pull-request", "1") == 1
    assert "signed commits" in capsys.readouterr().err
    config_path.write_text('[jj-stack]\nmerge_method = "squash"\n', encoding="utf-8")
    assert run_main(repo, config_path, "merge", "--dry-run", "--pull-request", "1") == 0
    assert "via squash" in capsys.readouterr().out
    assert fake_repo.stack_merge_requests == []

    fake_repo.allow_merge_commit = True
    exit_code = run_main(repo, config_path, "merge", "--method", "merge", "--pull-request", "1")
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert fake_repo.stack_merge_requests == [
        (1, "merge", "direct_merge", stack.changes[0].commit_id)
    ]
    assert fake_repo.prs[1].merged_at is not None
    assert fake_repo.prs[2].state == "open"


def test_merge_queue_lookup_failure_stops_before_requesting_a_merge(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    app = create_app(FakeGithubState.single_repo(fake_repo))

    class QueueLookupFailureClient(GithubClient):
        async def base_branch_uses_merge_queue(self, *, branch):
            raise GithubClientError(f"could not inspect {branch}")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=QueueLookupFailureClient,
    )

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == EXIT_GITHUB, (captured.out, captured.err)
    assert fake_repo.stack_merge_requests == []
    assert fake_repo.prs[1].merged_at is None
    assert_output_contains(captured.err, "uses a merge queue", "could not inspect main")


def test_merge_draft_blocks_the_candidate_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.prs[1].is_draft = True
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Merge blocked:" in captured.out
    assert "is now a draft" in captured.out
    assert fake_repo.prs[1].state == "open"
    assert fake_repo.prs[2].state == "open"
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before


def test_stack_merge_recovers_after_branch_cleanup_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = TrackingStore.for_repo(repo)
    head_change_id = selected_stack(repo).head.change_id
    mutate_refs = JjClient.mutate_remote_pr_branch_refs

    def fail_deletion(self, *, remote, updates):
        if any(update.desired_target is None for update in updates):
            raise CliError("Branch deletion connection failed")
        return mutate_refs(self, remote=remote, updates=updates)

    with monkeypatch.context() as failure:
        failure.setattr(JjClient, "mutate_remote_pr_branch_refs", fail_deletion)
        exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert read_remote_ref(fake_repo.git_dir, "main") == fake_repo.prs[1].merge_commit_sha
    jj = JjClient(repo)
    assert jj.query_commits_by_change_ids((head_change_id,))[head_change_id] == ()
    error = " ".join(captured.err.split())
    assert "Continue with jj-stack sync" not in error
    assert "jj-stack cleanup --pull-request 1" in error
    assert run_main(repo, config_path, "cleanup", "--pull-request", "1") == 0
    assert state_store.load().prs == {}
    assert fake_repo.ref_target(fake_repo.prs[1].head_ref) is None


def test_stack_merge_preserves_advanced_trunk_and_syncs_the_resolved_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.allow_merge_commit = True
    fake_repo.github_stacks = {7: (1, 2)}
    state_store = TrackingStore.for_repo(repo)
    stack = selected_stack(repo)
    advanced_trunk = fake_repo.advance_branch("main", path="upstream.txt", contents="upstream\n")

    merge_exit_code = run_main(
        repo,
        config_path,
        "merge",
        "--method",
        "merge",
        "heads(trunk()..@-)",
    )
    merged = capsys.readouterr()
    merge_commit = fake_repo.prs[1].merge_commit_sha

    assert merge_exit_code == 0, (merged.out, merged.err)
    assert merge_commit is not None
    assert fake_repo.prs[2].merge_commit_sha == merge_commit
    assert merge_commit == read_remote_ref(fake_repo.git_dir, "main")
    assert fake_repo.is_ancestor(advanced_trunk, merge_commit)
    assert all(fake_repo.is_ancestor(change.commit_id, merge_commit) for change in stack.changes)

    assert "Updating the local stack after the completed merge" in merged.out
    assert "submit" not in merged.out
    assert state_store.load().prs == {}
    assert JjClient(repo).resolve_commit("@").parents == (merge_commit,)
    assert (repo / "upstream.txt").read_text() == "upstream\n"


@pytest.mark.parametrize("merge_method", ("rebase", "squash"))
def test_stack_rewriting_merge_automatically_removes_pre_merge_copies(
    tmp_path: Path,
    monkeypatch,
    capsys,
    merge_method: str,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.allow_rebase_merge = True
    fake_repo.github_stacks = {7: (1, 2)}
    state_store = TrackingStore.for_repo(repo)
    stack = selected_stack(repo)
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=2,
    )
    assert any(
        STACK_OVERVIEW_COMMENT_MARKER in comment.body for comment in issue_comments(fake_repo, 2)
    )

    merge_exit_code = run_main(repo, config_path, "merge", "--method", merge_method)
    merged = capsys.readouterr()
    final_trunk = fake_repo.prs[2].merge_commit_sha

    assert merge_exit_code == 0, (merged.out, merged.err)
    assert final_trunk == read_remote_ref(fake_repo.git_dir, "main")

    assert "Updating the local stack after the completed merge" in merged.out
    assert state_store.load().prs == {}
    assert not any(
        ref.startswith("refs/heads/jj-stack/") for ref in remote_refs(fake_repo.git_dir)
    )
    assert not any(
        STACK_OVERVIEW_COMMENT_MARKER in comment.body for comment in issue_comments(fake_repo, 2)
    )
    assert JjClient(repo).resolve_commit("@").parents == (final_trunk,)
    copies = JjClient(repo).query_commits_by_change_ids(
        tuple(change.change_id for change in stack.changes)
    )
    if merge_method == "rebase":
        assert all(
            len(changes) == 1 and changes[0].immutable and not changes[0].divergent
            for changes in copies.values()
        )
    else:
        assert all(not changes for changes in copies.values())


def test_stack_merge_rejection_is_atomic_and_names_the_next_step(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.prs[2].merge_state_status = "BLOCKED"
    state_store = TrackingStore.for_repo(repo)
    state_before = state_store.load()
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    heads_before = tuple(fake_repo.ref_target(pr.head_ref) for pr in fake_repo.prs.values())
    head = selected_stack(repo).head.change_id[:8]

    # A rejection that is not a conflict says to fix it on GitHub and retry the same merge.
    fake_repo.merge_rejections[2] = "All comments must be resolved."
    assert run_main(repo, config_path, "merge") == 1
    rejected = " ".join(capsys.readouterr().out.split())
    assert "Merge blocked:" in rejected
    assert "All comments must be resolved." in rejected
    assert "resolved.." not in rejected
    assert "rebase" not in rejected
    assert f"jj-stack merge {head}" in rejected

    # A conflict cannot be cleared by retrying, so the hint routes the selected PR's change
    # through submit instead.
    bottom = selected_stack(repo).changes[0].change_id[:8]
    fake_repo.merge_rejections = {1: "Pull request #1 has merge conflicts"}
    assert run_main(repo, config_path, "merge", "--pull-request", "1") == 1
    conflicted = " ".join(capsys.readouterr().out.split())
    assert "has merge conflicts. " in conflicted
    assert "resolve the conflicts" in conflicted
    assert f"jj-stack submit {bottom}" in conflicted
    assert len(fake_repo.stack_merge_requests) == 2
    assert tuple(pr.state for pr in fake_repo.prs.values()) == ("open", "open")
    assert tuple(fake_repo.ref_target(pr.head_ref) for pr in fake_repo.prs.values()) == (
        heads_before
    )
    assert fake_repo.github_stacks == {7: (1, 2)}
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert state_store.load() == state_before


def test_stack_merge_resumes_a_matching_request_after_a_lost_response(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    state_store = TrackingStore.for_repo(repo)
    app = create_app(FakeGithubState.single_repo(fake_repo))

    class LostResponseClient(GithubClient):
        async def submit_stack_merge(
            self,
            *,
            expected_head_sha,
            merge_action,
            merge_method,
            pr_number,
        ):
            await super().submit_stack_merge(
                expected_head_sha=expected_head_sha,
                merge_action=merge_action,
                merge_method=merge_method,
                pr_number=pr_number,
            )
            raise GithubClientError("lost submit response")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=LostResponseClient,
    )
    assert run_main(repo, config_path, "merge") != 0
    capsys.readouterr()
    assert tuple(pr.state for pr in fake_repo.prs.values()) == ("open", "open")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
    )
    assert run_main(repo, config_path, "merge") == 0
    completed = capsys.readouterr()
    assert "Merge completed" in completed.out
    assert len(fake_repo.stack_merge_requests) == 1
    assert tuple(pr.state for pr in fake_repo.prs.values()) == ("closed", "closed")
    assert state_store.load().prs == {}
    assert JjClient(repo).resolve_commit("@").parents == (
        read_remote_ref(fake_repo.git_dir, "main"),
    )


def test_stack_merge_requires_a_resource_only_when_a_multi_pr_merge_can_proceed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {}
    state_store = TrackingStore.for_repo(repo)
    state_before = state_store.load()
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "did not report a stack" in captured.err
    assert fake_repo.stack_merge_requests == []
    assert tuple(pr.state for pr in fake_repo.prs.values()) == ("open", "open")
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert state_store.load() == state_before

    head_change_id = selected_stack(repo).head.change_id
    fake_repo.apply_squash_merge(fake_repo.prs[1])
    # GitHub commonly deletes a merged PR's branch, so the merged stop has to come from the pull
    # request itself, not from finding the branch where the last submit left it.
    delete_remote_ref(fake_repo, branch=fake_repo.prs[1].head_ref)

    retry_exit_code = run_main(repo, config_path, "merge")
    retry = capsys.readouterr()
    retry_rendered = " ".join((retry.out + retry.err).split())

    assert retry_exit_code == 1
    assert "is merged" in retry_rendered
    assert f"jj-stack sync {head_change_id[:8]}" in retry_rendered
    assert "submit" not in retry_rendered
    assert fake_repo.stack_merge_requests == []

    # A GitHub stack listing the merged PR cannot finish that merge from here either.
    fake_repo.github_stacks = {7: (1, 2)}
    assert run_main(repo, config_path, "merge") == 1
    listed = capsys.readouterr()
    listed_rendered = " ".join((listed.out + listed.err).split())
    assert f"jj-stack sync {head_change_id[:8]}" in listed_rendered
    assert "submit" not in listed_rendered
    assert fake_repo.stack_merge_requests == []


def test_merge_dry_run_ignores_closed_pr_for_reused_head_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    old_head_ref = fake_repo.prs[1].head_ref
    fake_repo.update_pr_state(fake_repo.prs[1], state="closed")
    assert run_main(repo, config_path, "cleanup", change_id) == 0
    assert run_main(repo, config_path, "submit", change_id) == 0
    capsys.readouterr()
    assert fake_repo.prs[2].head_ref == old_head_ref
    fake_repo.create_pr(
        base_ref="release",
        body="shared head",
        head_ref=old_head_ref,
        title="shared head",
    )
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    state_before = TrackingStore.for_repo(repo).load()

    exit_code = run_main(repo, config_path, "merge", "-p", "2", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Using PR #2 for change {change_id[:8]}\n" in captured.out
    assert "merge PR #2" in captured.out
    assert fake_repo.prs[1].state == "closed"
    assert fake_repo.prs[2].state == "open"
    assert fake_repo.prs[3].state == "open"
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert TrackingStore.for_repo(repo).load() == state_before


def test_merge_requires_submit_after_a_diff_equivalent_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change = selected_stack(repo).changes[0]
    state_store = TrackingStore.for_repo(repo)
    bookmark = state_store.load().prs[change.change_id].pr_identity.head_ref

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change.change_id, "-d", "main"], repo)
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    state_before = state_store.load()

    exit_code = run_main(repo, config_path, "merge", change.change_id)
    captured = capsys.readouterr()
    rendered = " ".join(captured.out.split())

    assert exit_code == 1
    assert "no longer matches the last submitted commit" in rendered
    assert f"jj-stack submit {change.change_id[:8]}" in rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert read_remote_ref(fake_repo.git_dir, bookmark) == change.commit_id
    assert fake_repo.prs[1].state == "open"
    assert state_store.load() == state_before


def test_merge_tells_a_conflicted_change_to_resolve_before_submitting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A conflicted change must not be told to submit, because submit refuses conflicts.

    Rebasing onto trunk to clear a merge refusal can itself conflict, so this is on the normal
    route out of a blocked merge. Reporting it as a commit mismatch and naming `submit` sent the
    user straight into a second, different failure.
    """

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).changes[0].change_id

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk conflict", "feature-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    exit_code = run_main(repo, config_path, "merge", change_id)
    rendered = " ".join(capsys.readouterr().out.split())

    assert exit_code == 1
    assert "unresolved conflicts" in rendered
    assert "resolve them" in rendered
    assert "no longer matches the last submitted commit" not in rendered
    assert fake_repo.prs[1].state == "open"


def test_merge_expected_head_guard_rejects_a_race(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change = selected_stack(repo).changes[0]
    state_before = TrackingStore.for_repo(repo).load()
    bookmark = state_before.prs[change.change_id].pr_identity.head_ref
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    fake_repo.auto_merge_reachable_heads = False
    app = create_app(FakeGithubState.single_repo(fake_repo))

    class HeadRaceClient(GithubClient):
        async def submit_stack_merge(
            self,
            *,
            expected_head_sha,
            merge_action,
            pr_number,
            merge_method,
        ):
            update_remote_ref(fake_repo, branch=bookmark, target=trunk_before)
            return await super().submit_stack_merge(
                expected_head_sha=expected_head_sha,
                merge_action=merge_action,
                pr_number=pr_number,
                merge_method=merge_method,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=HeadRaceClient,
    )

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PR head changed" in " ".join(captured.out.split())
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert fake_repo.prs[1].state == "open"
    assert fake_repo.prs[1].merged_at is None
    assert TrackingStore.for_repo(repo).load() == state_before
