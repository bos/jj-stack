from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore

from ..support.fake_github import FakeGithubState, _complete_stack_merge, create_app
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
)
from ..support.submit_property_harness import advance_remote_trunk, update_remote_ref
from .submit_command_helpers import (
    configure_submit_environment,
    patch_github_client_builders,
    read_remote_ref,
    run_main,
)

# Every case in this file is part of the bounded merge and post-merge convergence
# corpus described in docs/internals/property-testing.md.
pytestmark = pytest.mark.merge_recovery


def test_merge_accepts_a_stack_based_on_an_older_trunk(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Trunk moving under a reviewed stack is GitHub's call, not a reason for jj-stack to refuse.

    Unrelated work reaching trunk is routine in a busy repository. GitHub merges a reviewed
    pull request whose base is behind as long as it does not conflict, so refusing locally only
    forced a rebase and a force-push of an already-reviewed branch.
    """

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    advance_remote_trunk(fake_repo)

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "not based on the current" not in captured.out + captured.err
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].merged_at is not None


def test_merge_draft_blocks_the_candidate_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.pull_requests[1].is_draft = True
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Merge blocked:" in captured.out
    assert "is now a draft" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "open"
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before


def test_stack_merge_rebases_an_explicit_prefix_and_rewrites_the_survivor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.allow_rebase_merge = True
    fake_repo.github_stacks = {7: (1, 2, 3)}
    fake_repo.pull_requests[3].state = "closed"
    state_store = ReviewStateStore.for_repo(repo)
    stack_before = selected_stack(repo)
    state_before = state_store.load()
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    survivor_before = fake_repo.ref_target(fake_repo.pull_requests[3].head_ref)
    exit_code = run_main(
        repo,
        config_path,
        "merge",
        "--pull-request",
        "2",
        "--method",
        "rebase",
    )
    captured = capsys.readouterr()

    assert exit_code == 0, (captured.out, captured.err)
    assert fake_repo.stack_merge_requests == [(2, "rebase", stack_before.revisions[1].commit_id)]
    assert fake_repo.stack_merge_polls == [(2, "fake-stack-merge-1")]
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[3].state == "closed"
    assert fake_repo.pull_requests[3].base_ref == "main"
    assert fake_repo.ref_target(fake_repo.pull_requests[3].head_ref) != survivor_before
    assert read_remote_ref(fake_repo.git_dir, "main") != trunk_before
    assert "final trunk commit" in captured.out
    assert "sync " in captured.out
    assert state_store.load() == state_before
    assert tuple(revision.commit_id for revision in selected_stack(repo).revisions) == tuple(
        revision.commit_id for revision in stack_before.revisions
    )


def test_stack_merge_commit_uses_one_group_result_that_sync_can_retire(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.allow_merge_commit = True
    fake_repo.github_stacks = {7: (1, 2)}
    state_store = ReviewStateStore.for_repo(repo)
    stack = selected_stack(repo)

    merge_exit_code = run_main(
        repo,
        config_path,
        "merge",
        "--method",
        "merge",
    )
    merged = capsys.readouterr()
    merge_commit = fake_repo.pull_requests[1].merge_commit_sha

    assert merge_exit_code == 0, (merged.out, merged.err)
    assert merge_commit is not None
    assert fake_repo.pull_requests[2].merge_commit_sha == merge_commit
    assert merge_commit == read_remote_ref(fake_repo.git_dir, "main")
    assert all(
        fake_repo.is_ancestor(revision.commit_id, merge_commit) for revision in stack.revisions
    )

    sync_exit_code = run_main(repo, config_path, "sync", stack.head.change_id)
    synced = capsys.readouterr()

    assert sync_exit_code == 0, (synced.out, synced.err)
    assert state_store.load().review_identities == {}
    assert JjClient(repo).resolve_revision("@").only_parent_commit_id() == merge_commit


def test_stack_merge_terminal_failure_is_atomic(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    fake_repo.unmergeable_pull_numbers.add(2)
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    heads_before = tuple(
        fake_repo.ref_target(pr.head_ref) for pr in fake_repo.pull_requests.values()
    )

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "nothing merged" in captured.out
    # A refused group merge reaches the user the same way a refused single merge does: with the
    # rebase-and-resubmit route, since rerunning merge cannot clear a conflict.
    normalized = " ".join(captured.out.split())
    assert "rebase onto" in normalized
    assert "resolve the conflict" in normalized
    assert "jj-stack submit" in normalized
    assert fake_repo.stack_merge_requests
    assert fake_repo.stack_merge_polls == [(2, "fake-stack-merge-1")]
    assert tuple(pr.state for pr in fake_repo.pull_requests.values()) == ("open", "open")
    assert tuple(
        fake_repo.ref_target(pr.head_ref) for pr in fake_repo.pull_requests.values()
    ) == (heads_before)
    assert fake_repo.github_stacks == {7: (1, 2)}
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert state_store.load() == state_before


@pytest.mark.parametrize("dry_run", (False, True))
def test_stack_merge_reobserves_lower_heads_before_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
    dry_run: bool,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.auto_merge_reachable_heads = False
    fake_repo.github_stacks = {7: (1, 2)}
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class LowerHeadRaceClient(GithubClient):
        async def get_stack(self, *, stack_number):
            stack = await super().get_stack(stack_number=stack_number)
            update_remote_ref(
                fake_repo,
                branch=fake_repo.pull_requests[1].head_ref,
                target=trunk_before,
            )
            return stack

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.merge.command",),
        client_type=LowerHeadRaceClient,
    )

    exit_code = run_main(repo, config_path, "merge", *(("--dry-run",) if dry_run else ()))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "last submitted commit" in captured.err
    assert fake_repo.stack_merge_requests == []
    assert tuple(pr.state for pr in fake_repo.pull_requests.values()) == ("open", "open")
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before


def test_stack_merge_recovers_only_from_a_terminal_retry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {7: (1, 2)}
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class LostResponseClient(GithubClient):
        async def submit_stack_merge(
            self,
            *,
            expected_head_sha,
            merge_method,
            pull_number,
        ):
            await super().submit_stack_merge(
                expected_head_sha=expected_head_sha,
                merge_method=merge_method,
                pull_number=pull_number,
            )
            raise GithubClientError("lost submit response")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.merge.command",),
        client_type=LostResponseClient,
    )
    assert run_main(repo, config_path, "merge") != 0
    capsys.readouterr()
    assert fake_repo.stack_merge_polls == []
    assert tuple(pr.state for pr in fake_repo.pull_requests.values()) == ("open", "open")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.merge.command",),
    )
    assert run_main(repo, config_path, "merge") == 1
    pending = capsys.readouterr()
    assert "matching request is already pending" in pending.out
    assert fake_repo.stack_merge_polls == []
    assert tuple(pr.state for pr in fake_repo.pull_requests.values()) == ("open", "open")

    _complete_stack_merge(fake_repo, fake_repo.stack_merge_operations[2])
    assert tuple(pr.state for pr in fake_repo.pull_requests.values()) == ("closed", "closed")
    assert run_main(repo, config_path, "merge") == 0
    completed = capsys.readouterr()
    assert "final trunk commit" in completed.out
    assert fake_repo.stack_merge_polls == []
    assert len(fake_repo.stack_merge_requests) == 1
    assert tuple(pr.state for pr in fake_repo.pull_requests.values()) == ("closed", "closed")
    assert state_store.load() == state_before


def test_stack_merge_requires_a_resource_for_a_multi_pr_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    fake_repo.github_stacks = {}
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "did not report a stack" in captured.err
    assert fake_repo.stack_merge_requests == []
    assert tuple(pr.state for pr in fake_repo.pull_requests.values()) == ("open", "open")
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert state_store.load() == state_before


def test_ordinary_merge_methods_create_distinct_commit_topology(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Protect the history shape that later recovery observes from the fake."""

    observed: dict[str, tuple[bool, bool, bool, bool]] = {}
    for merge_method in ("merge", "rebase", "squash"):
        method_path = tmp_path / merge_method
        method_path.mkdir()
        repo, fake_repo = init_fake_github_repo_with_submitted_feature(method_path)
        config_path = configure_submit_environment(monkeypatch, method_path, fake_repo)
        fake_repo.allow_merge_commit = True
        fake_repo.allow_rebase_merge = True
        fake_repo.github_stacks = {}
        pull_request = fake_repo.pull_requests[1]
        head_before = fake_repo.ref_target(pull_request.head_ref)
        trunk_before = fake_repo.ref_target("main")
        assert head_before is not None
        assert trunk_before is not None

        exit_code = run_main(repo, config_path, "merge", "--method", merge_method)
        captured = capsys.readouterr()

        assert exit_code == 0, (captured.out, captured.err)
        assert pull_request.state == "closed"
        assert pull_request.merged_at is not None
        merge_result = pull_request.merge_commit_sha
        assert merge_result is not None
        parents = tuple(
            fake_repo._run_backing_git("show", "-s", "--format=%P", merge_result).split()
        )
        raw_commit = fake_repo._run_backing_git("cat-file", "commit", merge_result)
        observed[merge_method] = (
            parents == (trunk_before, head_before),
            parents == (trunk_before,),
            "\nchange-id " in raw_commit,
            fake_repo.is_ancestor(head_before, merge_result),
        )

    assert observed == {
        "merge": (True, False, False, True),
        "rebase": (False, True, True, False),
        "squash": (False, True, False, False),
    }


def test_merge_dry_run_validates_without_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    state_before = ReviewStateStore.for_repo(repo).load()

    exit_code = run_main(repo, config_path, "merge", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Planned merge actions:" in captured.out
    assert "merge PR #1" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert ReviewStateStore.for_repo(repo).load() == state_before


def test_merge_requires_submit_after_a_diff_equivalent_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    revision = selected_stack(repo).revisions[0]
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().review_identities[revision.change_id].head_ref

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", revision.change_id, "-d", "main"], repo)
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    state_before = state_store.load()

    exit_code = run_main(repo, config_path, "merge", revision.change_id)
    captured = capsys.readouterr()
    rendered = " ".join(captured.out.split())

    assert exit_code == 1
    assert "do not all name the same commit" in rendered
    assert f"jj-stack submit {revision.change_id[:8]}" in rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert read_remote_ref(fake_repo.git_dir, bookmark) == revision.commit_id
    assert fake_repo.pull_requests[1].state == "open"
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
    change_id = selected_stack(repo).revisions[0].change_id

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
    assert "do not all name the same commit" not in rendered
    assert fake_repo.pull_requests[1].state == "open"


def test_merge_expected_head_guard_rejects_a_race(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    revision = selected_stack(repo).revisions[0]
    state_before = ReviewStateStore.for_repo(repo).load()
    bookmark = state_before.review_identities[revision.change_id].head_ref
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    fake_repo.auto_merge_reachable_heads = False
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class HeadRaceClient(GithubClient):
        async def merge_pull_request(
            self,
            *,
            expected_head_sha,
            pull_number,
            merge_method,
        ):
            update_remote_ref(fake_repo, branch=bookmark, target=trunk_before)
            return await super().merge_pull_request(
                expected_head_sha=expected_head_sha,
                pull_number=pull_number,
                merge_method=merge_method,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.merge.command",),
        client_type=HeadRaceClient,
    )

    exit_code = run_main(repo, config_path, "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PR head changed" in " ".join(captured.out.split())
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[1].merged_at is None
    assert ReviewStateStore.for_repo(repo).load() == state_before
