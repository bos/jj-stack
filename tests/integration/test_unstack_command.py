from __future__ import annotations

import json
from pathlib import Path

import jj_stack.commands.unstack as unstack_module
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import STACK_NAVIGATION_COMMENT_MARKER
from jj_stack.jj.client import JjClient, ReviewRefUpdate
from jj_stack.state.store import ReviewStateStore, resolve_state_path

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
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    patch_github_client_builders,
    read_remote_ref,
    remote_refs,
    run_main,
)


def _combined_output(captured) -> str:
    return " ".join((captured.out + " " + captured.err).split())


def test_unstack_apply_closes_pull_request_and_preserves_exact_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_before = state_store.load()
    app = create_app(FakeGithubState.single_repository(fake_repo))
    operations: list[str] = []
    native_members = (1, 2)
    locked = True
    fake_repo.native_stacks = {7: native_members}
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)

    class NativeStackClient(GithubClient):
        async def unstack(self, *, stack_number):
            operations.append("unstack")
            return github_stack(1) if locked else None

        async def close_pull_request(self, *, pull_number):
            operations.append("close")
            return await super().close_pull_request(pull_number=pull_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.unstack", "jj_stack.review.status"),
        client_type=NativeStackClient,
    )

    dry_run_exit_code = run_main(repo, config_path, "unstack", "--dry-run", change_id)
    dry_run = capsys.readouterr()

    assert dry_run_exit_code == 1
    assert "keeps #2 active outside the selected stack" in _combined_output(dry_run)
    assert "gh stack unstack 7" in _combined_output(dry_run)
    assert operations == [] and fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == state_before

    blocked_exit_code = run_main(repo, config_path, "unstack", change_id)
    blocked = capsys.readouterr()

    assert blocked_exit_code == 1
    assert "keeps #2 active outside the selected stack" in _combined_output(blocked)
    assert operations == [] and fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == state_before

    native_members = (1,)
    fake_repo.native_stacks = {7: native_members}
    locked_exit_code = run_main(repo, config_path, "unstack", change_id)
    locked_run = capsys.readouterr()

    assert locked_exit_code == 1
    assert "GitHub stack #7 still contains #1" in _combined_output(locked_run)
    assert operations == ["unstack"] and fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == state_before

    locked = False
    exit_code = run_main(repo, config_path, "unstack", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert refreshed_state == state_before
    assert issue_comments(fake_repo, 1) == []
    assert operations == ["unstack", "unstack", "close"]


def test_unstack_dissolves_active_suffix_and_retains_historical_prefix(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)
    fake_repo.native_stacks = {7: (1, 2)}
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.pull_requests[1].merged_at = "2026-07-23T12:00:00Z"

    exit_code = run_main(repo, config_path, "unstack", stack.head.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "dissolve GitHub stack #7" in captured.out
    assert fake_repo.native_stacks == {7: (1,)}
    assert fake_repo.pull_requests[1].merged_at is not None
    assert fake_repo.pull_requests[2].state == "closed"


def test_unstack_dissolves_a_resource_that_dropped_a_closed_selected_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A selected review GitHub no longer lists as a member must not block the dissolve."""

    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = JjClient(repo).discover_review_stack()
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)
    fake_repo.pull_requests[1].state = "closed"
    fake_repo.native_stacks = {7: (2,)}

    exit_code = run_main(repo, config_path, "unstack", stack.head.change_id)
    captured = capsys.readouterr()

    assert exit_code == 0, _combined_output(captured)
    assert "dissolve GitHub stack #7" in captured.out
    assert fake_repo.native_stacks == {}
    assert fake_repo.pull_requests[1].merged_at is None
    assert fake_repo.pull_requests[2].state == "closed"


def test_unstack_head_change_before_native_boundary_preserves_github_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    change_id = JjClient(repo).discover_review_stack().head.change_id
    fake_repo.native_stacks = {7: (1,)}
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)
    app = create_app(FakeGithubState.single_repository(fake_repo))
    native_observations = 0
    fresh_boundary_lookups = 0
    native_mutations: list[int] = []

    class HeadRaceClient(GithubClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.native_observed = False

        async def list_stacks(self):
            nonlocal native_observations
            stacks = await super().list_stacks()
            native_observations += 1
            self.native_observed = True
            return stacks

        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            nonlocal fresh_boundary_lookups
            if self.native_observed:
                fresh_boundary_lookups += 1
                fake_repo.pull_requests[1].head_ref = "jj-stack/moved-aaaaaaaa"
                fake_repo.pull_requests[1].head_label = "octo-org:jj-stack/moved-aaaaaaaa"
            return await super().get_pull_requests_by_numbers(pull_numbers=pull_numbers)

        async def unstack(self, *, stack_number):
            native_mutations.append(stack_number)
            return await super().unstack(stack_number=stack_number)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.unstack", "jj_stack.review.status"),
        client_type=HeadRaceClient,
    )

    exit_code = run_main(repo, config_path, "unstack", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "live PR and head no longer uniquely match" in _combined_output(captured)
    assert native_observations == 1
    assert fresh_boundary_lookups == 1
    assert native_mutations == []
    assert fake_repo.native_stacks == {7: (1,)}
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == initial_state


def test_unstack_local_forgets_tracking_without_closing_pull_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    branch = state_store.load().review_identities[change_id].head_ref

    exit_code = run_main(repo, config_path, "unstack", "--local", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied local unstack actions:" in captured.out
    assert "forget local review tracking for feature 1" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert change_id not in state_store.load().review_identities
    assert read_remote_ref(fake_repo.git_dir, branch)


def test_unstack_local_dry_run_leaves_tracking_and_pull_request_unchanged(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()

    exit_code = run_main(repo, config_path, "unstack", "--local", "--dry-run", change_id)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Planned local unstack actions:" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.load() == initial_state


def test_unstack_observes_review_branches_without_importing_them(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    original_fetch_remote = JjClient.fetch_remote
    fetch_calls: list[str] = []

    def tracking_fetch_remote(
        self,
        *,
        remote: str,
        dry_run: bool = False,
        on_isolation_change=None,
    ) -> None:
        fetch_calls.append(remote)
        return original_fetch_remote(
            self,
            remote=remote,
            dry_run=dry_run,
            on_isolation_change=on_isolation_change,
        )

    monkeypatch.setattr(
        "jj_stack.review.status.JjClient.fetch_remote",
        tracking_fetch_remote,
    )

    assert run_main(repo, config_path, "unstack", "--dry-run") == 0
    capsys.readouterr()
    assert fetch_calls == []

    assert run_main(repo, config_path, "unstack") == 0
    capsys.readouterr()
    assert fetch_calls == []

    assert run_main(repo, config_path, "unstack", "--cleanup") == 0
    capsys.readouterr()
    assert fetch_calls == []


def test_unstack_apply_can_select_a_stack_by_pull_request_number(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    first_change_id = stack.revisions[0].change_id
    second_change_id = stack.revisions[1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    first_pr_number = initial_state.review_identities[first_change_id].pr_number
    second_pr_number = initial_state.review_identities[second_change_id].pr_number

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--pull-request",
        str(first_pr_number),
    )
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert f"Using PR #{first_pr_number} -> {first_change_id}" in captured.out
    assert fake_repo.pull_requests[first_pr_number].state == "closed"
    assert fake_repo.pull_requests[second_pr_number].state == "open"
    assert refreshed_state == initial_state


def test_unstack_cleanup_pull_request_without_saved_record_reports_open_pr_not_tracked(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    pull_request = fake_repo.create_pull_request(
        base_ref="main",
        body="",
        head_ref="jj-stack/untracked",
        title="untracked",
    )

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(pull_request.number),
    )
    captured = capsys.readouterr()
    combined = _combined_output(captured)

    assert exit_code == 1
    assert f"PR #{pull_request.number} is not tracked locally" in combined
    assert "not linked to any local change" not in combined


def test_unstack_and_cleanup_match_dry_run_on_fully_untracked_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    git_root = Path(run_command(["jj", "git", "root"], repo).stdout.strip())
    fetch_config_before = run_command(
        [
            "git",
            "--git-dir",
            str(git_root),
            "config",
            "--get-all",
            "remote.origin.fetch",
        ],
        repo,
    ).stdout

    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()

    original_fetch_remote = JjClient.fetch_remote
    fetch_calls: list[str] = []

    def tracking_fetch_remote(
        self,
        *,
        remote: str,
        dry_run: bool = False,
        on_isolation_change=None,
    ) -> None:
        fetch_calls.append(remote)
        return original_fetch_remote(
            self,
            remote=remote,
            dry_run=dry_run,
            on_isolation_change=on_isolation_change,
        )

    async def fail_list_stacks(*args, **kwargs):
        raise AssertionError("unstack should not inspect native stacks without a mutation")

    monkeypatch.setattr(
        "jj_stack.review.status.JjClient.fetch_remote",
        tracking_fetch_remote,
    )
    monkeypatch.setattr(GithubClient, "list_stacks", fail_list_stacks)

    dry_run_exit_code = run_main(repo, config_path, "unstack", "--dry-run")
    dry_run_captured = capsys.readouterr()
    close_exit_code = run_main(repo, config_path, "unstack")
    close_captured = capsys.readouterr()
    cleanup_exit_code = run_main(repo, config_path, "unstack", "--cleanup")
    cleanup_captured = capsys.readouterr()

    assert dry_run_exit_code == 0
    assert close_exit_code == 0
    assert cleanup_exit_code == 0
    assert "Nothing to close on the selected stack." in dry_run_captured.out
    assert "Nothing to close on the selected stack." in close_captured.out
    assert "Nothing to close on the selected stack." in cleanup_captured.out
    assert "Reserved jj-stack/" not in cleanup_captured.out
    assert state_store.load() == initial_state
    assert fetch_calls == []
    assert (
        run_command(
            [
                "git",
                "--git-dir",
                str(git_root),
                "config",
                "--get-all",
                "remote.origin.fetch",
            ],
            repo,
        ).stdout
        == fetch_config_before
    )


def test_unstack_dry_run_leaves_remote_state_unchanged_and_reports_planned_actions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)
    fake_repo.native_stacks = {7: (1,)}
    initial_state = state_store.load()

    exit_code = run_main(repo, config_path, "unstack", "--dry-run", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "Planned close actions:" in captured.out
    assert "dissolve GitHub stack #7" in captured.out
    assert fake_repo.native_stacks == {7: (1,)}
    assert fake_repo.pull_requests[1].state == "open"
    assert refreshed_state == initial_state
    assert issue_comments(fake_repo, 1) == []


def test_unstack_pull_request_selector_requires_a_linked_local_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    resolve_state_path(repo).unlink()

    exit_code = run_main(repo, config_path, "unstack", "--pull-request", "1")
    captured = capsys.readouterr()
    combined_output = _combined_output(captured)

    assert exit_code == 1
    assert "PR #1 is not linked to any local change." in combined_output
    assert fake_repo.pull_requests[1].state == "open"


def test_unstack_apply_reports_blocked_when_github_is_unavailable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    initial_state = ReviewStateStore.for_repo(repo).load()
    app = create_app(FakeGithubState.single_repository(fake_repo))

    class OfflineGithubClient(GithubClient):
        async def list_pull_requests(self, *, head, state="all"):
            raise GithubClientError("Connection refused")

        async def list_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError("Connection refused")

        async def get_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.unstack", "jj_stack.review.status"),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "unstack", change_id)
    captured = capsys.readouterr()
    combined_output = _combined_output(captured)

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "Applied close actions:" not in captured.out
    assert "cannot close pull requests tracked by jj-stack without live GitHub state" in (
        combined_output
    )
    assert ReviewStateStore.for_repo(repo).load() == initial_state


def test_unstack_apply_cleanup_deletes_review_branches_comments_and_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    branches = tuple(
        state.review_identities[revision.change_id].head_ref for revision in stack.revisions
    )
    external = fake_repo.create_pull_request(
        base_ref=branches[0],
        body="",
        head_ref="manual/external-dependent",
        title="external dependent",
    )
    action_order: list[str] = []
    original_mutate_review_refs = JjClient.mutate_remote_review_refs

    def tracking_mutate_review_refs(
        self,
        *,
        remote: str,
        updates,
    ) -> None:
        action_order.extend(f"remote:{update.branch}" for update in updates)
        return original_mutate_review_refs(
            self,
            remote=remote,
            updates=updates,
        )

    monkeypatch.setattr(
        JjClient,
        "mutate_remote_review_refs",
        tracking_mutate_review_refs,
    )

    dry_run_exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--dry-run",
        change_id,
    )
    dry_run = capsys.readouterr()

    assert dry_run_exit_code == 1
    assert f"open PR #{external.number} still" in dry_run.out
    assert all(fake_repo.pull_requests[number].state == "open" for number in (1, 2))
    assert state_store.load() == state
    del fake_repo.pull_requests[external.number]

    exit_code = run_main(repo, config_path, "unstack", "--cleanup", change_id)
    captured = capsys.readouterr()
    refreshed_state = state_store.load()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "Applied close actions:" in captured.out
    assert normalized_output.count("stop review tracking for feature") == 2
    assert all(
        pull_request.state == "closed" for pull_request in fake_repo.pull_requests.values()
    )
    assert refreshed_state.review_identities == {}
    assert refreshed_state.submitted_baselines == {}
    assert all(issue_comments(fake_repo, number) == [] for number in (1, 2))
    assert all(branch not in remote_refs(fake_repo.git_dir) for branch in branches)
    assert action_order == [
        f"remote:{branches[1]}",
        f"remote:{branches[0]}",
    ]


def test_unstack_cleanup_rechecks_dependents_after_comment_discovery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    change_id, identity = next(iter(state.review_identities.items()))
    find_comments = unstack_module._find_managed_comments

    async def dependent_appears_during_comment_lookup(**kwargs):
        lookups = await find_comments(**kwargs)
        fake_repo.create_pull_request(
            base_ref=identity.head_ref,
            body="",
            head_ref="manual/late-dependent",
            title="late dependent",
        )
        return lookups

    monkeypatch.setattr(
        unstack_module,
        "_find_managed_comments",
        dependent_appears_during_comment_lookup,
    )
    exit_code = run_main(repo, config_path, "unstack", "--cleanup", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "open PR #2 still" in captured.out
    assert fake_repo.pull_requests[1].state == "closed"
    assert state_store.load() == state
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_unstack_cleanup_pull_request_retires_orphaned_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    orphaned_change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    orphaned_bookmark = state.review_identities[orphaned_change_id].head_ref
    orphaned_pr_number = state.review_identities[orphaned_change_id].pr_number

    run_command(["jj", "abandon", orphaned_change_id], repo)

    async def dissolve_exact(_selection, **_kwargs):
        return github_stack(orphaned_pr_number)

    monkeypatch.setattr(GithubStackSelection, "dissolve_exact", dissolve_exact)

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(orphaned_pr_number),
    )
    captured = capsys.readouterr()
    refreshed_state = state_store.load()
    output = captured.out

    assert exit_code == 0
    assert "Applied close actions:" in output
    assert "dissolve GitHub stack #7" in output
    assert f"close PR #{orphaned_pr_number}" in output
    assert "prune orphan record" in output
    assert fake_repo.pull_requests[orphaned_pr_number].state == "closed"
    assert issue_comments(fake_repo, orphaned_pr_number) == []
    assert orphaned_change_id not in refreshed_state.review_identities
    assert orphaned_bookmark not in remote_refs(fake_repo.git_dir)


def test_unstack_cleanup_orphans_retries_base_after_dependent_closes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmarks = tuple(
        initial_state.review_identities[change_id].head_ref for change_id in change_ids
    )

    run_command(["jj", "abandon", *change_ids], repo)

    dry_run_exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        "orphans",
        "--dry-run",
    )
    dry_run = capsys.readouterr()

    assert dry_run_exit_code == 1
    assert "open PR #2 still" in dry_run.out
    assert "close PR #1" not in dry_run.out
    assert "close PR #2" in dry_run.out
    assert all(pull_request.state == "open" for pull_request in fake_repo.pull_requests.values())
    assert state_store.load() == initial_state

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        "orphans",
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "open PR #2 still" in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
    assert set(state_store.load().review_identities) == {change_ids[0]}

    rerun_exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        "orphans",
    )
    rerun = capsys.readouterr()

    assert rerun_exit_code == 0
    assert "close PR #1" in rerun.out
    assert all(
        pull_request.state == "closed" for pull_request in fake_repo.pull_requests.values()
    )
    assert state_store.load().review_identities == {}
    remaining_remote_refs = remote_refs(fake_repo.git_dir)
    assert all(f"refs/heads/{bookmark}" not in remaining_remote_refs for bookmark in bookmarks)


def test_unstack_cleanup_orphans_continues_after_one_orphan_is_blocked(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_ids = tuple(revision.change_id for revision in stack.revisions)
    state_store = ReviewStateStore.for_repo(repo)
    fake_repo.pull_requests[2].base_ref = "main"
    run_command(["jj", "abandon", *change_ids], repo)

    async def dissolve_exact(selection, **_kwargs):
        if tuple(selection.pull_numbers) == (1,):
            raise CliError("native stack includes an unselected pull request")
        return None

    monkeypatch.setattr(GithubStackSelection, "dissolve_exact", dissolve_exact)

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        "orphans",
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "native stack includes an unselected pull request" in _combined_output(captured)
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
    refreshed_state = state_store.load()
    assert change_ids[0] in refreshed_state.review_identities
    assert change_ids[1] not in refreshed_state.review_identities


def test_unstack_cleanup_orphans_rejects_pr_claimed_by_live_change_before_mutation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    orphaned_change_id = stack.revisions[0].change_id
    live_change_id = stack.revisions[1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    orphaned_pr_number = state.review_identities[orphaned_change_id].pr_number
    run_command(["jj", "abandon", orphaned_change_id], repo)
    live_identity = state.review_identities[live_change_id]
    live_baseline = state.submitted_baselines[live_change_id]
    state_store.relink_review(
        live_change_id,
        expected_identity=live_identity,
        expected_baseline=live_baseline,
        identity=live_identity.model_copy(update={"pr_number": orphaned_pr_number}),
        baseline=live_baseline,
    )
    ambiguous_state = state_store.load()

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        "orphans",
    )
    captured = capsys.readouterr()

    assert exit_code == 6
    assert "multiple tracking records claim the same PR" in _combined_output(captured)
    assert all(pull_request.state == "open" for pull_request in fake_repo.pull_requests.values())
    assert state_store.load() == ambiguous_state


def test_unstack_cleanup_pull_request_closes_orphaned_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    orphaned_change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    orphaned_bookmark = state.review_identities[orphaned_change_id].head_ref
    orphaned_pr_number = state.review_identities[orphaned_change_id].pr_number

    run_command(["jj", "abandon", orphaned_change_id], repo)
    fake_repo.pull_requests[orphaned_pr_number].state = "closed"

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(orphaned_pr_number),
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "prune orphan record" in captured.out
    assert orphaned_change_id not in state_store.load().review_identities
    assert orphaned_bookmark not in remote_refs(fake_repo.git_dir)


def test_unstack_orphan_rechecks_native_membership_before_branch_delete(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    change_id = JjClient(repo).discover_review_stack().head.change_id
    bookmark = initial_state.review_identities[change_id].head_ref
    run_command(["jj", "abandon", change_id], repo)
    fake_repo.native_stacks = {7: (1,)}
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)
    app = create_app(FakeGithubState.single_repository(fake_repo))
    observations = 0

    class NativeMembershipRaceClient(GithubClient):
        async def list_stacks(self):
            nonlocal observations
            observations += 1
            if observations == 2:
                fake_repo.native_stacks = {8: (1,)}
            return await super().list_stacks()

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.close_orphan", "jj_stack.commands.unstack"),
        client_type=NativeMembershipRaceClient,
    )

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        "1",
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert observations == 2
    assert "dissolve GitHub stack #7" in captured.out
    assert "GitHub stack #8 blocks this jj-stack operation" in _combined_output(captured)
    assert fake_repo.native_stacks == {8: (1,)}
    assert fake_repo.pull_requests[1].state == "closed"
    assert state_store.load() == initial_state
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_unstack_cleanup_pull_request_blocks_when_saved_submitted_target_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    bottom_bookmark = state.review_identities[bottom_change_id].head_ref
    bottom_pr_number = state.review_identities[bottom_change_id].pr_number

    run_command(["jj", "abandon", bottom_change_id], repo)
    state_path = resolve_state_path(repo)
    raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_state["submitted_baselines"].pop(bottom_change_id)
    state_path.write_text(
        json.dumps(raw_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(bottom_pr_number),
    )
    captured = capsys.readouterr()
    combined = _combined_output(captured)

    assert exit_code == 1
    assert "no valid last submitted commit" in combined
    assert f"close PR #{bottom_pr_number}" not in captured.out
    assert fake_repo.pull_requests[bottom_pr_number].state == "open"
    assert issue_comments(fake_repo, bottom_pr_number)
    assert f"refs/heads/{bottom_bookmark}" in remote_refs(fake_repo.git_dir)
    assert bottom_change_id in state_store.load().review_identities


def test_unstack_cleanup_pull_request_blocks_when_saved_target_drifted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    orphaned_change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    orphaned_bookmark = state.review_identities[orphaned_change_id].head_ref
    orphaned_pr_number = state.review_identities[orphaned_change_id].pr_number

    run_command(["jj", "abandon", orphaned_change_id], repo)
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{orphaned_bookmark}",
            read_remote_ref(fake_repo.git_dir, "main"),
        ],
        fake_repo.git_dir.parent,
    )

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(orphaned_pr_number),
    )
    captured = capsys.readouterr()
    combined = _combined_output(captured)

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "head no longer matches the saved submitted commit" in combined
    assert f"close PR #{orphaned_pr_number}" not in captured.out
    assert orphaned_change_id in state_store.load().review_identities


def test_unstack_cleanup_pull_request_blocks_when_remote_branch_drifted_externally(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    head_change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    bottom_bookmark = state.review_identities[bottom_change_id].head_ref
    bottom_pr_number = state.review_identities[bottom_change_id].pr_number
    head_target = state.submitted_baselines[head_change_id].commit_id

    run_command(["jj", "abandon", bottom_change_id], repo)
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{bottom_bookmark}",
            head_target,
        ],
        fake_repo.git_dir.parent,
    )

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(bottom_pr_number),
    )
    captured = capsys.readouterr()
    combined = _combined_output(captured)

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "head no longer matches the saved submitted commit" in combined
    assert f"close PR #{bottom_pr_number}" not in captured.out
    assert fake_repo.pull_requests[bottom_pr_number].state == "open"
    assert issue_comments(fake_repo, bottom_pr_number)
    assert bottom_change_id in state_store.load().review_identities


def test_unstack_cleanup_pull_request_blocks_when_saved_pr_is_no_longer_on_github(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    bottom_bookmark = state.review_identities[bottom_change_id].head_ref
    bottom_pr_number = state.review_identities[bottom_change_id].pr_number

    run_command(["jj", "abandon", bottom_change_id], repo)
    del fake_repo.pull_requests[bottom_pr_number]

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(bottom_pr_number),
    )
    captured = capsys.readouterr()
    combined_output = _combined_output(captured)

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert f"PR #{bottom_pr_number} is no longer on GitHub" in combined_output
    assert f"refs/heads/{bottom_bookmark}" in remote_refs(fake_repo.git_dir)
    assert bottom_change_id in state_store.load().review_identities


def test_unstack_cleanup_pull_request_reports_blocked_when_github_is_unavailable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().revisions[-1].change_id
    initial_state = ReviewStateStore.for_repo(repo).load()
    app = create_app(FakeGithubState.single_repository(fake_repo))
    run_command(["jj", "abandon", change_id], repo)

    class OfflineGithubClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            raise GithubClientError("Connection refused")

        async def get_pull_requests_by_head_refs(self, *, head_refs):
            raise GithubClientError("Connection refused")

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_stack.commands.unstack", "jj_stack.commands.close_orphan"),
        client_type=OfflineGithubClient,
    )

    exit_code = run_main(repo, config_path, "unstack", "--cleanup", "--pull-request", "1")
    captured = capsys.readouterr()
    combined_output = _combined_output(captured)

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "cannot inspect pull requests tracked by jj-stack without live GitHub state" in (
        combined_output
    )
    assert ReviewStateStore.for_repo(repo).load() == initial_state
    assert fake_repo.pull_requests[1].state == "open"


def test_unstack_cleanup_pull_request_keeps_tracking_when_cleanup_blocks(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = JjClient(repo).discover_review_stack().head.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.review_identities[change_id].head_ref

    fake_repo.pull_requests[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{bookmark}",
            read_remote_ref(fake_repo.git_dir, "main"),
        ],
        fake_repo.git_dir.parent,
    )

    exit_code = run_main(repo, config_path, "unstack", "--cleanup", "--pull-request", "1")
    captured = capsys.readouterr()
    combined_output = _combined_output(captured)
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "head no longer matches the saved submitted commit" in combined_output
    assert refreshed_state == initial_state


def test_unstack_cleanup_pull_request_dry_run_rejects_partial_native_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    bottom_change_id = stack.revisions[0].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state_store.set_stacked_pull_requests("octo-org/stacked-review", True)
    fake_repo.native_stacks = {7: (1, 2)}
    state = state_store.load()
    bottom_bookmark = state.review_identities[bottom_change_id].head_ref
    bottom_pr_number = state.review_identities[bottom_change_id].pr_number

    fake_repo.pull_requests[2].base_ref = "main"
    run_command(["jj", "abandon", bottom_change_id], repo)

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--dry-run",
        "--pull-request",
        str(bottom_pr_number),
    )
    captured = capsys.readouterr()
    output = captured.out

    assert exit_code == 1
    assert "Close blocked:" in output
    assert "keeps #2 active outside the selected stack" in _combined_output(captured)
    assert fake_repo.pull_requests[bottom_pr_number].state == "open"
    assert f"refs/heads/{bottom_bookmark}" in remote_refs(fake_repo.git_dir)
    assert state_store.load() == state


def test_unstack_cleanup_pull_request_orphan_close_is_idempotent_after_branch_already_gone(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    orphaned_change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    orphaned_bookmark = state.review_identities[orphaned_change_id].head_ref
    orphaned_pr_number = state.review_identities[orphaned_change_id].pr_number

    run_command(["jj", "abandon", orphaned_change_id], repo)
    last_target = state.submitted_baselines[orphaned_change_id].commit_id
    JjClient(repo).mutate_remote_review_refs(
        remote="origin",
        updates=(
            ReviewRefUpdate(
                branch=orphaned_bookmark,
                expected_target=last_target,
                desired_target=None,
            ),
        ),
    )

    exit_code = run_main(
        repo,
        config_path,
        "unstack",
        "--cleanup",
        "--pull-request",
        str(orphaned_pr_number),
    )
    captured = capsys.readouterr()
    output = captured.out

    assert exit_code == 0
    assert "Applied close actions:" in output
    assert "prune orphan record" in output
    assert fake_repo.pull_requests[orphaned_pr_number].state == "closed"
    assert orphaned_change_id not in state_store.load().review_identities


def test_unstack_apply_blocks_when_github_no_longer_reports_the_cached_pull_request(
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
    del fake_repo.pull_requests[1]

    exit_code = run_main(repo, config_path, "unstack", change_id)
    captured = capsys.readouterr()
    combined_output = _combined_output(captured)

    assert exit_code == 1
    assert "GitHub no longer reports a pull request" in combined_output
    assert state_store.load() == initial_state


def test_unstack_apply_checkpoints_prior_progress_before_later_block(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    first_change_id = stack.revisions[0].change_id
    head_change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    first_bookmark = initial_state.review_identities[first_change_id].head_ref
    head_pr_number = initial_state.review_identities[head_change_id].pr_number

    fake_repo.create_pull_request(
        base_ref="main",
        body="duplicate",
        head_ref=first_bookmark,
        title="feature 1 duplicate",
    )

    first_exit_code = run_main(repo, config_path, "unstack", head_change_id)
    first_run = capsys.readouterr()
    checkpointed_state = state_store.load()

    second_exit_code = run_main(repo, config_path, "unstack", head_change_id)
    second_run = capsys.readouterr()

    assert first_exit_code == 1
    assert second_exit_code == 1
    assert "Close blocked:" in first_run.out
    assert checkpointed_state == initial_state
    assert fake_repo.pull_requests[1].state == "open"
    assert fake_repo.pull_requests[2].state == "closed"
    assert "previous close was interrupted" not in second_run.out
    assert f"close PR #{head_pr_number}" not in second_run.out


def test_unstack_apply_requires_checkout_after_sparse_state_loss(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    resolve_state_path(repo).unlink()

    exit_code = run_main(repo, config_path, "unstack", change_id)
    captured = capsys.readouterr()
    refreshed_state = ReviewStateStore.for_repo(repo).load()

    assert exit_code == 0
    assert "Nothing to close on the selected stack." in captured.out
    assert fake_repo.pull_requests[1].state == "open"
    assert refreshed_state.review_identities == {}


def test_unstack_apply_cleanup_exits_nonzero_when_cleanup_is_blocked(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.review_identities[change_id].head_ref
    fake_repo.create_issue_comment(
        body=f"{STACK_NAVIGATION_COMMENT_MARKER}\nextra",
        issue_number=2,
    )
    initial_comments = issue_comments(fake_repo, 2)

    exit_code = run_main(repo, config_path, "unstack", "--cleanup", change_id)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Close blocked:" in captured.out
    assert "stack navigation comment:" in captured.out
    assert fake_repo.pull_requests[2].state == "closed"
    assert issue_comments(fake_repo, 2) == initial_comments
    assert (
        state_store.load().review_identities[change_id]
        == (initial_state.review_identities[change_id])
    )
    assert (
        state_store.load().submitted_baselines[change_id]
        == (initial_state.submitted_baselines[change_id])
    )
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)
