from __future__ import annotations

import json
from pathlib import Path

import pytest

import jj_stack.commands.land.command as land_command
from jj_stack.errors import EXIT_INCOMPLETE
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.state.store import ReviewStateStore

from ..support.fake_github import (
    FakeGithubState,
    create_app,
    github_stack,
)
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    write_file,
)
from ..support.submit_property_harness import advance_remote_trunk, update_remote_ref
from .submit_command_helpers import (
    approve_pull_requests,
    configure_submit_environment,
    patch_github_client_builders,
    read_remote_ref,
    run_main,
)

_LAND_CLIENT_MODULES = (
    "jj_stack.commands.land.command",
    "jj_stack.commands.sync",
    "jj_stack.review.landed",
)


def _squash_whitespace(text: str) -> str:
    return " ".join(text.split())


def test_land_blocks_unlinked_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    assert run_main(repo, config_path, "unlink", change_id) == 0
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "land", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    rendered = _squash_whitespace(captured.out)
    assert "Land blocked:" in rendered
    assert "unlinked from review tracking" in rendered


def test_land_previews_and_finalizes_maximal_ready_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    bookmark_1 = submitted_state.review_identities[change_id_1].head_ref
    bookmark_2 = submitted_state.review_identities[change_id_2].head_ref

    fake_repo.pull_requests[3].state = "closed"

    preview_exit_code = run_main(repo, config_path, "land", "--dry-run")
    preview = capsys.readouterr()
    rendered_preview = _squash_whitespace(preview.out)

    assert preview_exit_code == 0
    assert "push main to feature 2" in rendered_preview
    assert "finish landed PR #1" in rendered_preview
    assert "finish landed PR #2" in rendered_preview
    assert f"forget {bookmark_1}" in rendered_preview
    assert f"forget {bookmark_2}" in rendered_preview
    assert "before feature 3" in rendered_preview

    apply_exit_code = run_main(repo, config_path, "land")
    applied = capsys.readouterr()
    rendered_applied = _squash_whitespace(applied.out)

    assert apply_exit_code == 0
    assert "Finalizing PR #2 for feature 2" in rendered_applied
    assert f"forget {bookmark_1}" in rendered_applied
    assert f"forget {bookmark_2}" in rendered_applied
    assert "remove tracking for landed feature 1" in rendered_applied
    assert "remove tracking for landed feature 2" in rendered_applied
    assert read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[1].commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[3].state == "closed"
    bookmark_states = JjClient(repo).list_bookmark_states((bookmark_1, bookmark_2))
    assert bookmark_states[bookmark_1].local_target is None
    assert bookmark_states[bookmark_2].local_target is None
    assert read_remote_ref(fake_repo.git_dir, bookmark_1) == stack.revisions[0].commit_id
    assert read_remote_ref(fake_repo.git_dir, bookmark_2) == stack.revisions[1].commit_id

    landed_state = state_store.load()
    assert change_id_1 not in landed_state.review_identities
    assert change_id_1 not in landed_state.submitted_baselines
    assert change_id_2 not in landed_state.review_identities
    assert change_id_2 not in landed_state.submitted_baselines
    list_exit_code = run_main(repo, config_path, "list", "--json")
    listed = capsys.readouterr()
    assert list_exit_code in (0, EXIT_INCOMPLETE)
    listed_change_ids = _list_json_change_ids(listed.out)
    assert change_id_1 not in listed_change_ids
    assert change_id_2 not in listed_change_ids
    assert change_id_3 in listed_change_ids


def _list_json_change_ids(list_output: str) -> set[str]:
    """Every change id `list --json` reports, across stack and orphan rows."""

    rows = json.loads(list_output).get("rows", ())
    change_ids = {change["change_id"] for row in rows for change in row.get("changes", ())}
    change_ids.update(row["change_id"] for row in rows if row.get("type") == "orphan")
    return change_ids


def test_land_skip_cleanup_keeps_landed_local_review_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id = stack.revisions[0].change_id
    bookmark = submitted_state.review_identities[change_id].head_ref

    exit_code = run_main(repo, config_path, "land", "--skip-cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"forget local bookmark {bookmark}" not in captured.out
    bookmark_state = JjClient(repo).get_bookmark_state(bookmark)
    assert bookmark_state.local_target == stack.revisions[0].commit_id
    landed_state = state_store.load()
    assert change_id not in landed_state.review_identities
    assert change_id not in landed_state.submitted_baselines


def test_land_rejects_stack_forked_from_trunk_ancestor(
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

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1)

    original_fetch_remote = JjClient.fetch_remote
    fetch_calls: list[str] = []

    def tracking_fetch_remote(self, *, remote: str, branches=None) -> None:
        fetch_calls.append(remote)
        return original_fetch_remote(self, remote=remote, branches=branches)

    monkeypatch.setattr(
        "jj_stack.review.status.JjClient.fetch_remote",
        tracking_fetch_remote,
    )

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error: Selected stack is not based on the current trunk()." in captured.err
    assert "\nHint: No change in the selected stack has landed yet." in captured.err
    assert "jj rebase -s" in captured.err
    assert fetch_calls == ["origin"]


def test_land_reports_current_trunk_drift_after_fetch_instead_of_bookmark_mismatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    other = tmp_path / "other"
    run_command(["git", "clone", str(fake_repo.git_dir), str(other)], tmp_path)
    run_command(["git", "config", "user.name", "Other User"], other)
    run_command(["git", "config", "user.email", "other@example.com"], other)
    write_file(other / "trunk-1.txt", "trunk 1\n")
    run_command(["git", "add", "trunk-1.txt"], other)
    run_command(["git", "commit", "-m", "trunk 1"], other)
    run_command(["git", "push", "origin", "HEAD:main"], other)

    exit_code = run_main(repo, config_path, "land", "--dry-run")
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "Error: Selected stack is not based on the current trunk()." in captured.err
    assert "\nHint: No change in the selected stack has landed yet." in captured.err
    assert "jj rebase -s" in captured.err
    assert "Local bookmark main points to a different revision" not in combined


def test_land_blocks_unapproved_prefix_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "PR #1 is not approved" in captured.out


def test_land_pull_request_selects_the_landed_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2, 3)

    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    change_id_2 = stack.revisions[1].change_id
    change_id_3 = stack.revisions[2].change_id
    bookmark_3 = submitted_state.review_identities[change_id_3].head_ref

    exit_code = run_main(repo, config_path, "land", "--pull-request", "2")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0
    assert f"Using PR #2 -> {change_id_2}" in rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[1].commit_id
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    assert fake_repo.pull_requests[3].state == "open"
    assert JjClient(repo).get_bookmark_state(bookmark_3).local_target is not None


def test_land_bypass_readiness_previews_and_finalizes_unapproved_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()

    preview_exit_code = run_main(
        repo,
        config_path,
        "land",
        "--bypass-readiness",
        "--dry-run",
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert "push main to feature 1" in preview.out

    apply_exit_code = run_main(
        repo,
        config_path,
        "land",
        "--bypass-readiness",
    )
    capsys.readouterr()

    assert apply_exit_code == 0
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert read_remote_ref(fake_repo.git_dir, "main") == stack.revisions[0].commit_id


@pytest.mark.landing_recovery
def test_land_requires_submit_after_diff_equivalent_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[0].change_id
    old_commit_id = stack.revisions[0].commit_id
    submitted_state = ReviewStateStore.for_repo(repo).load()
    bookmark = submitted_state.review_identities[change_id].head_ref

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "trunk 1", "trunk-1.txt")
    run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
    run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
    run_command(["jj", "rebase", "-s", change_id, "-d", "main"], repo)

    rebased_stack = JjClient(repo).discover_review_stack(change_id)
    rebased_commit_id = rebased_stack.revisions[0].commit_id
    trunk_target = read_remote_ref(fake_repo.git_dir, "main")
    state_before_land = ReviewStateStore.for_repo(repo).load()

    assert rebased_commit_id != old_commit_id
    assert read_remote_ref(fake_repo.git_dir, bookmark) == old_commit_id

    preview_exit_code = run_main(repo, config_path, "land", "--dry-run", change_id)
    preview = capsys.readouterr()

    preview_output = _squash_whitespace(preview.out)

    assert preview_exit_code == 1
    assert "do not all identify the same exact commit" in preview_output
    assert f"jj-stack submit {change_id}" in preview_output
    assert read_remote_ref(fake_repo.git_dir, bookmark) == old_commit_id

    apply_exit_code = run_main(repo, config_path, "land", change_id)
    applied = capsys.readouterr()

    applied_output = _squash_whitespace(applied.out)

    assert apply_exit_code == 1
    assert "do not all identify the same exact commit" in applied_output
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_target
    assert read_remote_ref(fake_repo.git_dir, bookmark) == old_commit_id
    assert fake_repo.pull_requests[1].state == "open"
    assert ReviewStateStore.for_repo(repo).load() == state_before_land


def test_land_blocks_unresolved_conflicted_rebase(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "shared.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 1)

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

    exit_code = run_main(repo, config_path, "land", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "still has unresolved conflicts" in _squash_whitespace(captured.out)


def test_land_rerun_after_failed_push_replans_from_current_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A failed trunk push leaves no residue; the rerun replans against live state."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    initial_stack = JjClient(repo).discover_review_stack()
    first_landable_commit_id = initial_stack.revisions[0].commit_id
    trunk_before = JjClient(repo).get_bookmark_state("main").local_target
    remote_before = read_remote_ref(fake_repo.git_dir, "main")

    push_calls = 0
    original_push_bookmark_with_lease = JjClient.push_bookmark_with_lease

    def fail_first_push_bookmark_with_lease(
        self,
        *,
        remote_target: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        nonlocal push_calls
        push_calls += 1
        if push_calls == 1:
            raise JjCommandError("simulated trunk push failure")
        original_push_bookmark_with_lease(
            self,
            remote_target=remote_target,
            bookmark=bookmark,
            desired_target=desired_target,
            expected_remote_target=expected_remote_target,
        )

    monkeypatch.setattr(
        JjClient,
        "push_bookmark_with_lease",
        fail_first_push_bookmark_with_lease,
    )

    first_exit_code = run_main(repo, config_path, "land")
    first_run = capsys.readouterr()

    assert first_exit_code == 1
    assert "simulated trunk push failure" in first_run.err
    assert JjClient(repo).get_bookmark_state("main").local_target == trunk_before
    assert read_remote_ref(fake_repo.git_dir, "main") == remote_before

    fake_repo.pull_requests[2].state = "closed"

    second_exit_code = run_main(repo, config_path, "land")
    second_run = capsys.readouterr()

    assert second_exit_code == 0, (second_run.out, second_run.err)
    assert "push main to feature 1" in _squash_whitespace(second_run.out)
    assert read_remote_ref(fake_repo.git_dir, "main") == first_landable_commit_id


@pytest.mark.landing_recovery
def test_land_rechecks_exact_review_head_before_direct_push(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    client = JjClient(repo)
    stack = client.discover_review_stack()
    change_id = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    bookmark = state_before.review_identities[change_id].head_ref
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    injected = False
    stack_checks = 0
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class LateHeadMoveClient(GithubClient):
        async def list_stacks(self, *, pull_number=None):
            nonlocal stack_checks
            stack_checks += 1
            return (github_stack(1, 2),) if stack_checks == 1 else ()

        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            nonlocal injected
            if stack_checks and not injected:
                injected = True
                update_remote_ref(
                    fake_repo,
                    branch=bookmark,
                    target=stack.revisions[1].commit_id,
                )
            return await super().get_pull_requests_by_numbers(pull_numbers=pull_numbers)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=LateHeadMoveClient,
    )

    blocked_exit_code = run_main(repo, config_path, "land")
    blocked = capsys.readouterr()

    assert blocked_exit_code == 1
    assert "GitHub stack #7 blocks this jj-stack operation" in _squash_whitespace(
        blocked.err
    )
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == state_before

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert "last submitted commit" in _squash_whitespace(captured.out)
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert client.get_bookmark_state("main").local_target == trunk_before
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == state_before


@pytest.mark.landing_recovery
def test_land_does_not_overwrite_a_concurrent_local_trunk_move(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    client = JjClient(repo)
    stack = client.discover_review_stack()
    landed_commit = stack.revisions[0].commit_id
    concurrent_target = stack.revisions[1].commit_id
    original_fetch = JjClient.fetch_remote
    injected = False

    def fetch_then_move_local_trunk(self, *, remote, branches=None) -> None:
        nonlocal injected
        original_fetch(self, remote=remote, branches=branches)
        if not injected and read_remote_ref(fake_repo.git_dir, "main") == landed_commit:
            injected = True
            self.set_bookmark("main", concurrent_target, allow_backwards=True)

    monkeypatch.setattr(JjClient, "fetch_remote", fetch_then_move_local_trunk)

    run_main(repo, config_path, "land")
    capsys.readouterr()

    assert injected
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit
    assert client.get_bookmark_state("main").local_target == concurrent_target


@pytest.mark.landing_recovery
def test_land_exact_lease_rejects_concurrent_trunk_move(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    client = JjClient(repo)
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    original_push = JjClient.push_bookmark_with_lease
    concurrent_trunk: str | None = None

    def race_trunk_then_push(self, **kwargs) -> None:
        nonlocal concurrent_trunk
        advance_remote_trunk(fake_repo)
        concurrent_trunk = read_remote_ref(fake_repo.git_dir, "main")
        original_push(self, **kwargs)

    monkeypatch.setattr(JjClient, "push_bookmark_with_lease", race_trunk_then_push)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert concurrent_trunk is not None
    assert read_remote_ref(fake_repo.git_dir, "main") == concurrent_trunk
    assert client.get_bookmark_state("main").local_target == concurrent_trunk
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == state_before


@pytest.mark.landing_recovery
def test_land_reauthorizes_after_retarget_before_closing_direct_push_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    fake_repo.auto_merge_reachable_heads = False
    stack = JjClient(repo).discover_review_stack()
    second = stack.revisions[1]
    state_store = ReviewStateStore.for_repo(repo)
    second_identity = state_store.load().review_identities[second.change_id]
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class HeadMovesAfterRetargetClient(GithubClient):
        async def update_pull_request(self, **kwargs):
            pull_request = await super().update_pull_request(**kwargs)
            if kwargs["pull_number"] == second_identity.pr_number:
                update_remote_ref(
                    fake_repo,
                    branch=second_identity.head_ref,
                    target=stack.revisions[0].commit_id,
                )
            return pull_request

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=HeadMovesAfterRetargetClient,
    )

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert fake_repo.pull_requests[2].base_ref == "main"
    assert fake_repo.pull_requests[2].state == "open"
    assert all(
        not (event.pull_request_number == 2 and event.new_state == "closed")
        for event in fake_repo.pull_request_events
    )
    assert second.change_id in state_store.load().review_identities


@pytest.mark.landing_recovery
def test_land_rechecks_duplicate_saved_identity_claims_after_planning(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    first, second = JjClient(repo).discover_review_stack().revisions
    state_store = ReviewStateStore.for_repo(repo)
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    original_execute = land_command.execute_land_plan

    async def duplicate_claim_then_execute(**kwargs):
        state = state_store.load()
        second_identity = state.review_identities[second.change_id]
        second_baseline = state.submitted_baselines[second.change_id]
        state_store.relink_review(
            second.change_id,
            expected_identity=second_identity,
            expected_baseline=second_baseline,
            identity=second_identity.model_copy(
                update={"pr_number": state.review_identities[first.change_id].pr_number}
            ),
            baseline=second_baseline,
        )
        return await original_execute(**kwargs)

    monkeypatch.setattr(land_command, "execute_land_plan", duplicate_claim_then_execute)

    exit_code = run_main(repo, config_path, "land", first.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert "multiple saved changes" in _squash_whitespace(captured.out + captured.err)
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert fake_repo.pull_requests[1].state == "open"
    state = state_store.load()
    assert state.review_identities[first.change_id].pr_number == 1
    assert state.review_identities[second.change_id].pr_number == 1


def test_land_finishes_after_trunk_push_interrupted_before_finalization(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Finalization left behind by an interrupted land converges through sync."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=3)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    landed_commit_id = stack.revisions[1].commit_id
    landed_change_ids = tuple(revision.change_id for revision in stack.revisions[:2])
    state_store = ReviewStateStore.for_repo(repo)
    submitted_state = state_store.load()
    bookmarks = tuple(
        submitted_state.review_identities[change_id].head_ref for change_id in landed_change_ids
    )
    saved_bookmarks = bookmarks

    app = create_app(FakeGithubState.single_repository(fake_repo))
    original_push = JjClient.push_bookmark_with_lease

    class FailOnFinalizeLoadClient(GithubClient):
        armed = False

        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            if self.armed and 1 in pull_numbers:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_requests_by_numbers(pull_numbers=pull_numbers)

    def push_then_arm_finalization_fault(self, **kwargs) -> None:
        original_push(self, **kwargs)
        FailOnFinalizeLoadClient.armed = True

    monkeypatch.setattr(
        JjClient,
        "push_bookmark_with_lease",
        push_then_arm_finalization_fault,
    )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnFinalizeLoadClient,
    )

    first_exit_code = run_main(repo, config_path, "land")
    first_run = capsys.readouterr()
    first_rendered = _squash_whitespace(first_run.out)

    assert first_exit_code == 1
    assert "Simulated finalization failure" in first_rendered
    assert "jj-stack sync --all" in first_rendered
    assert "Finalizing PR #2 for feature 2" in first_rendered
    assert read_remote_ref(fake_repo.git_dir, "main") == landed_commit_id
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    interrupted_state = state_store.load()
    assert landed_change_ids[0] in interrupted_state.review_identities
    assert landed_change_ids[0] in interrupted_state.submitted_baselines
    assert landed_change_ids[1] not in interrupted_state.review_identities
    assert landed_change_ids[1] not in interrupted_state.submitted_baselines

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )

    # Repository-wide recovery is explicit and exact-snapshot-only.
    rerun_exit_code = run_main(repo, config_path, "sync", "--all")
    rerun = capsys.readouterr()
    rerun_rendered = _squash_whitespace(rerun.out)

    assert "remove tracking" in rerun_rendered
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    interrupted_state = state_store.load()
    assert landed_change_ids[0] not in interrupted_state.review_identities
    assert landed_change_ids[0] not in interrupted_state.submitted_baselines
    bookmark_states = JjClient(repo).list_bookmark_states(saved_bookmarks)
    for bookmark in saved_bookmarks:
        assert bookmark_states[bookmark].local_target is None
    assert rerun_exit_code == 0

    # sync then refreshes the surviving stack normally.
    sync_exit_code = run_main(repo, config_path, "sync")
    sync_run = capsys.readouterr()
    assert sync_exit_code == 0, (sync_run.out, sync_run.err)
    assert fake_repo.pull_requests[3].base_ref == "main"


@pytest.mark.landing_recovery
def test_sync_preserves_merged_review_whose_saved_repository_no_longer_matches(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A merged PR is never retired after its saved repository identity changes."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    commit_1 = stack.revisions[0].commit_id
    change_id_1 = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-20T12:00:00Z"
    update_remote_ref(fake_repo, branch="main", target=commit_1)
    state = state_store.load()
    identity = state.review_identities[change_id_1]
    baseline = state.submitted_baselines[change_id_1]
    state_store.relink_review(
        change_id_1,
        expected_identity=identity,
        expected_baseline=baseline,
        identity=identity.model_copy(update={"repository_owner": "other-org"}),
        baseline=baseline,
    )

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 1, (captured.out, captured.err)
    assert "no longer matches the pull request recorded for" in rendered
    state = state_store.load()
    assert change_id_1 in state.review_identities
    assert change_id_1 in state.submitted_baselines


@pytest.mark.landing_recovery
def test_selected_sync_preserves_landed_review_with_local_edits_since_submit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A landed review whose change was edited locally is reported, never retired.

    The dangerous variant: another workspace's direct push put the exact
    submitted commit on trunk while its PR is still open, and the user edited
    the change here before fetching. Convergence must not close that PR or
    retire its tracking out from under the edits.
    """

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    commit_1 = stack.revisions[0].commit_id
    state_store = ReviewStateStore.for_repo(repo)
    # The exact submitted commit reaches trunk remotely (as an interrupted
    # direct push from elsewhere would leave it); PR #1 stays open.
    update_remote_ref(fake_repo, branch="main", target=commit_1)
    # The user edits the change locally before fetching.
    run_command(["jj", "describe", "-r", change_id_1, "-m", "feature 1 edited"], repo)

    exit_code = run_main(repo, config_path, "sync")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out + captured.err)

    # Remote finalization succeeds independently, but local rewrite and retirement stop.
    assert exit_code == 1, (captured.out, captured.err)
    assert "unpublished local edits since submit" in rendered
    # PR state is not a usable signal here: the fake auto-marks reachable
    # heads merged (see its idealization note). The contract is that the
    # landed handling neither finalized nor retired the edited review.
    state = state_store.load()
    assert change_id_1 in state.review_identities
    assert change_id_1 in state.submitted_baselines


@pytest.mark.landing_recovery
def test_land_with_clean_plan_does_not_touch_an_unrelated_straggler(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Selected direct landing never touches an unrelated exact-on-trunk review."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)

    app = create_app(FakeGithubState.single_repository(fake_repo))
    original_push = JjClient.push_bookmark_with_lease

    class FailOnFinalizeLoadClient(GithubClient):
        armed = False

        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            if self.armed and 1 in pull_numbers:
                raise GithubClientError("Simulated finalization failure", status_code=500)
            return await super().get_pull_requests_by_numbers(pull_numbers=pull_numbers)

    def push_then_arm_finalization_fault(self, **kwargs) -> None:
        original_push(self, **kwargs)
        FailOnFinalizeLoadClient.armed = True

    monkeypatch.setattr(
        JjClient,
        "push_bookmark_with_lease",
        push_then_arm_finalization_fault,
    )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=FailOnFinalizeLoadClient,
    )
    assert run_main(repo, config_path, "land") == 1
    capsys.readouterr()
    state = state_store.load()
    assert change_id_1 in state.review_identities
    assert change_id_1 in state.submitted_baselines

    # A lost tracking save can leave an already-closed exact-on-trunk review.
    # A later selected land leaves that unrelated residue alone.
    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
    )
    fake_repo.pull_requests[1].state = "closed"
    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    approve_pull_requests(fake_repo, 2)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "skip landed" not in rendered
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    # The terminal straggler is neither mutated nor retired by selected land.
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is None
    state = state_store.load()
    assert change_id_1 in state.review_identities
    assert change_id_1 in state.submitted_baselines


def test_sync_retires_review_merged_outside_the_tool_with_preserved_commit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A merge that preserved the exact commit retires lazily on the next sync."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    commit_1 = stack.revisions[0].commit_id
    change_id_1 = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark_1 = state_store.load().review_identities[change_id_1].head_ref
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-03-16T12:00:00Z"
    update_remote_ref(fake_repo, branch="main", target=commit_1)

    exit_code = run_main(repo, config_path, "sync", "--all")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "remove tracking" in rendered
    state = state_store.load()
    assert change_id_1 not in state.review_identities
    assert change_id_1 not in state.submitted_baselines
    assert JjClient(repo).list_bookmark_states((bookmark_1,))[bookmark_1].local_target is None


def test_land_via_merge_merges_ready_prefix_bottom_up_on_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Merge-transport landing converges the local stack before returning."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    stack = JjClient(repo).discover_review_stack()
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    original_main = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()
    rendered = _squash_whitespace(captured.out)

    assert exit_code == 0, (captured.out, captured.err)
    assert "merge PR #1" in rendered
    assert "merge PR #2" in rendered
    assert "Nothing to submit: everything in this stack has landed." in rendered
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"
    assert fake_repo.pull_requests[2].merged_at is not None
    # The second PR was retargeted to trunk before merging.
    assert fake_repo.pull_requests[2].base_ref == "main"
    # GitHub's trunk moved through the merges; jj-stack never pushed it.
    assert read_remote_ref(fake_repo.git_dir, "main") != original_main
    # The merged changes were reconciled out of tracking by the convergence run.
    state = ReviewStateStore.for_repo(repo).load()
    for change_id in change_ids:
        assert change_id not in state.review_identities
        assert change_id not in state.submitted_baselines


@pytest.mark.landing_recovery
def test_land_via_merge_reports_an_accepted_prefix_when_trunk_refresh_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    revision, selected = JjClient(repo).discover_review_stack().revisions
    original_fetch = JjClient.fetch_remote
    failed = False

    def fail_first_post_merge_fetch(self, *, remote, branches=None) -> None:
        nonlocal failed
        if fake_repo.pull_requests[1].merged_at is not None and not failed:
            failed = True
            raise JjCommandError("lost post-merge fetch")
        original_fetch(self, remote=remote, branches=branches)

    monkeypatch.setattr(JjClient, "fetch_remote", fail_first_post_merge_fetch)

    exit_code = run_main(repo, config_path, "land", "--via", "merge", selected.change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert failed
    assert "merge PR #1" in captured.out
    assert "after accepted" in captured.out
    assert selected.change_id[:8] in captured.out
    assert "live trunk ref moved" in captured.err
    assert revision.change_id in ReviewStateStore.for_repo(repo).load().review_identities


@pytest.mark.landing_recovery
def test_land_via_merge_rechecks_readiness_after_retarget(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class DismissAfterRetargetClient(GithubClient):
        async def update_pull_request(
            self,
            *,
            pull_number,
            base=None,
            body=None,
            title=None,
        ):
            pull_request = await super().update_pull_request(
                pull_number=pull_number,
                base=base,
                body=body,
                title=title,
            )
            if pull_number == 2:
                fake_repo.create_pull_request_review(
                    pull_number=2,
                    reviewer_login="late-reviewer",
                    state="CHANGES_REQUESTED",
                )
            return pull_request

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=DismissAfterRetargetClient,
    )

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert "no longer ready" in _squash_whitespace(captured.out)
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "open"
    assert fake_repo.pull_requests[2].merged_at is None


@pytest.mark.landing_recovery
def test_land_via_merge_expected_head_guard_rejects_race(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    revision = JjClient(repo).discover_review_stack().revisions[0]
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    bookmark = state_before.review_identities[revision.change_id].head_ref
    trunk_before = read_remote_ref(fake_repo.git_dir, "main")
    app = create_app(FakeGithubState.single_repository(fake_repo))
    fake_repo.auto_merge_reachable_heads = False
    stack_checks = 0

    class HeadRaceClient(GithubClient):
        async def list_stacks(self, *, pull_number=None):
            nonlocal stack_checks
            stack_checks += 1
            return (github_stack(1),) if stack_checks == 1 else ()

        async def merge_pull_request(
            self,
            *,
            expected_head_sha,
            pull_number,
            merge_method,
        ):
            update_remote_ref(fake_repo, branch=bookmark, target=trunk_before)
            await super().merge_pull_request(
                expected_head_sha=expected_head_sha,
                pull_number=pull_number,
                merge_method=merge_method,
            )

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=_LAND_CLIENT_MODULES,
        client_type=HeadRaceClient,
    )

    blocked_exit_code = run_main(repo, config_path, "land", "--via", "merge")
    blocked = capsys.readouterr()

    assert blocked_exit_code == 1
    assert "GitHub stack #7 blocks this jj-stack operation" in _squash_whitespace(
        blocked.err
    )
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == state_before

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 1, (captured.out, captured.err)
    assert "PR head changed" in _squash_whitespace(captured.out)
    assert read_remote_ref(fake_repo.git_dir, "main") == trunk_before
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[1].merged_at is None
    assert state_store.load() == state_before


def test_land_via_merge_stops_fail_closed_then_converges_accepted_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1, 2)
    fake_repo.unmergeable_pull_numbers.add(2)
    stack = JjClient(repo).discover_review_stack()
    change_id_1 = stack.revisions[0].change_id
    change_id_2 = stack.revisions[1].change_id
    original_commit_2 = stack.revisions[1].commit_id

    exit_code = run_main(repo, config_path, "land", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not mergeable" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "open"
    assert fake_repo.pull_requests[2].merged_at is None
    # The accepted prefix converged: its tracking retired, and the surviving
    # change was rebased onto the merged trunk and resubmitted.
    state = ReviewStateStore.for_repo(repo).load()
    assert change_id_1 not in state.review_identities
    assert change_id_1 not in state.submitted_baselines
    surviving_commit = JjClient(repo).resolve_revision(change_id_2).commit_id
    assert surviving_commit != original_commit_2
    assert state.submitted_baselines[change_id_2].commit_id == surviving_commit
    assert fake_repo.pull_requests[2].base_ref == "main"


def test_land_via_merge_dry_run_previews_merges_without_mutating(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    original_main = read_remote_ref(fake_repo.git_dir, "main")

    exit_code = run_main(repo, config_path, "land", "--dry-run", "--via", "merge")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "merge PR #1" in captured.out
    assert "Planned land actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert read_remote_ref(fake_repo.git_dir, "main") == original_main


def test_land_classifies_protected_branch_push_rejection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A GH006 rejection surfaces the reason and the matching next step."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    approve_pull_requests(fake_repo, 1)
    original_trunk_target = JjClient(repo).get_bookmark_state("main").local_target
    hooks_dir = fake_repo.git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        'echo "GH006: Protected branch update failed for refs/heads/main." >&2\n'
        'echo "7 of 7 required status checks are expected." >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    exit_code = run_main(repo, config_path, "land")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GH006: Protected branch update failed" in captured.err
    assert "required status checks are expected" in captured.err
    assert "Wait for the review-branch checks to finish" in captured.err
    assert "land --via merge would not help" in captured.err
    # The failed push left the local trunk bookmark and PR intact.
    assert JjClient(repo).get_bookmark_state("main").local_target == original_trunk_target
    assert fake_repo.pull_requests[1].state == "open"
