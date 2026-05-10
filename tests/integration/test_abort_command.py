from __future__ import annotations

import os
from pathlib import Path

from jj_review.github.resolution import ParsedGithubRepo
from jj_review.jj import JjClient
from jj_review.models.intent import CleanupRebaseIntent, SubmitIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.state.intents import write_new_intent
from jj_review.state.journal import OperationJournal, read_journal
from jj_review.state.store import ReviewStateStore

from ..support.fake_github import initialize_bare_repository
from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    run_command,
)
from ..support.output_assertions import assert_output_contains
from .submit_command_helpers import (
    configure_submit_environment,
    read_remote_ref,
    remote_refs,
    run_main,
)


def test_abort_reports_nothing_when_no_intent_file_exists(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Nothing to abort" in captured.out


def test_abort_dry_run_shows_planned_actions_without_mutating(
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

    # Inject a stale submit intent to simulate an interrupted submit.
    from jj_review.models.intent import SubmitIntent

    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        ordered_commit_ids=(stack.revisions[-1].commit_id,),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Planned abort actions" in captured.out
    assert "close PR" in captured.out
    assert f"{bookmark}@origin" in captured.out
    assert bookmark in captured.out
    # Nothing was actually mutated.
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests[1].state == "open"
    # Intent file still present after dry-run.
    assert state_store.list_intents()


def test_abort_retracts_submitted_change_and_clears_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    # Inject a submit intent referencing the live change.
    from jj_review.models.intent import SubmitIntent

    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        ordered_commit_ids=(stack.revisions[-1].commit_id,),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied abort actions" in captured.out
    assert "close PR" in captured.out
    assert f"{bookmark}@origin" in captured.out
    assert bookmark in captured.out

    # PR was closed on GitHub.
    assert fake_repo.pull_requests[1].state == "closed"

    # Remote branch was deleted.
    assert bookmark not in remote_refs(fake_repo.git_dir)

    # Saved state was cleared.
    refreshed = state_store.load()
    assert change_id not in refreshed.changes

    # Intent file was removed.
    assert not state_store.list_intents()


def test_abort_refuses_submit_retraction_after_stack_rewrite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    revision = stack.revisions[-1]
    change_id = revision.change_id
    state_store = ReviewStateStore.for_repo(repo)
    initial_state = state_store.load()
    bookmark = initial_state.changes[change_id].bookmark
    assert bookmark is not None
    initial_remote_target = read_remote_ref(fake_repo.git_dir, bookmark)

    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        ordered_commit_ids=(revision.commit_id,),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    run_command(["jj", "describe", "-r", change_id, "-m", "feature 1 rewritten"], repo)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Abort incomplete" in captured.out
    assert_output_contains(
        captured.out,
        "abort will not guess",
        "current stack has changed since this submit",
    )
    assert "notice: kept" in captured.out
    assert state_store.load() == initial_state
    assert read_remote_ref(fake_repo.git_dir, bookmark) == initial_remote_target
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.list_intents()


def test_abort_clears_submit_record_when_recorded_head_was_abandoned(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    commit_file(repo, "feature 1", "feature-1.txt")
    commit_file(repo, "feature 2", "feature-2.txt")
    bottom, head = JjClient(repo).discover_review_stack().revisions
    state_store = ReviewStateStore.for_repo(repo)
    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label=f"submit on {head.change_id[:8]}",
        display_revset=head.change_id[:8],
        ordered_commit_ids=(bottom.commit_id, head.commit_id),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(bottom.change_id, head.change_id),
        bookmarks={
            bottom.change_id: f"review/change-{bottom.change_id[:8]}",
            head.change_id: f"review/change-{head.change_id[:8]}",
        },
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    run_command(["jj", "abandon", head.change_id], repo)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "Applied abort actions" in captured.out
    assert "github: no pull requests or review branches were changed" in normalized_output
    assert f"change {head.change_id[:8]} is no longer visible in jj" in normalized_output
    assert "cannot safely" not in normalized_output
    assert "jj-review cleanup" not in captured.out
    assert f"close --cleanup {head.change_id[:8]}" not in normalized_output
    assert not state_store.list_intents()


def test_abort_keeps_local_bookmark_when_remote_bookmark_is_conflicted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from jj_review.commands import abort as abort_module
    from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    revision = stack.revisions[-1]
    change_id = revision.change_id
    commit_id = revision.commit_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    write_new_intent(
        state_store.state_dir,
        SubmitIntent(
            kind="submit",
            pid=99999999,
            label=f"submit on {change_id[:8]}",
            display_revset=change_id[:8],
            ordered_commit_ids=(commit_id,),
            remote_name="origin",
            github_host="github.test",
            github_owner="octo-org",
            github_repo="stacked-review",
            ordered_change_ids=(change_id,),
            bookmarks={change_id: bookmark},
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )

    real_get_bookmark_state = abort_module.JjClient.get_bookmark_state

    def _conflicted_get_bookmark_state(self, name: str):
        if name == bookmark:
            return BookmarkState(
                name=bookmark,
                local_targets=(commit_id,),
                remote_targets=(
                    RemoteBookmarkState(
                        remote="origin",
                        targets=(commit_id, "other-commit"),
                    ),
                ),
            )
        return real_get_bookmark_state(self, name)

    monkeypatch.setattr(
        abort_module.JjClient,
        "get_bookmark_state",
        _conflicted_get_bookmark_state,
    )

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 1
    assert "conflicted" in normalized_output
    assert "remote branch" in normalized_output
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)
    bookmark_state = JjClient(repo).get_bookmark_state(bookmark)
    assert bookmark_state.local_target == commit_id
    assert change_id in state_store.load().changes
    assert state_store.list_intents()


def test_abort_removes_cleanup_restack_intent_with_note(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.trunk.change_id

    state_store = ReviewStateStore.for_repo(repo)
    state_store.require_writable()
    intent = CleanupRebaseIntent(
        kind="cleanup-rebase",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label="cleanup --rebase on @-",
        display_revset="@-",
        ordered_change_ids=(change_id,),
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied abort actions" in captured.out
    assert "cleared it from future status output" in captured.out
    assert "rebase" in captured.out  # note about manual inspection
    assert not state_store.list_intents()


def test_abort_clears_land_journal_with_note(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    revision = stack.revisions[0]
    state_store = ReviewStateStore.for_repo(repo)
    journal = OperationJournal.begin(
        state_store.require_writable(),
        operation="land",
        lock_holder=None,
        options={
            "bypass_readiness": False,
            "cleanup_bookmarks": True,
            "selected_pr_number": None,
        },
        resolved_scope={
            "github_repository": "octo-org/stacked-review",
            "landed_change_ids": (revision.change_id,),
            "landed_commit_id": revision.commit_id,
            "ordered_change_ids": (revision.change_id,),
            "ordered_commit_ids": (revision.commit_id,),
            "planned_change_ids": (revision.change_id,),
            "planned_revisions": (
                {
                    "bookmark": "review/feature",
                    "bookmark_managed": True,
                    "change_id": revision.change_id,
                    "commit_id": revision.commit_id,
                    "pull_request_number": 1,
                    "subject": revision.subject,
                },
            ),
            "push_trunk": True,
            "remote_name": "origin",
            "selected_revset": "@-",
            "trunk_branch": "main",
        },
    )

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied abort actions" in captured.out
    assert "Landing cannot be retracted" in captured.out
    assert not state_store.list_operations()
    assert read_journal(journal.path)[-1].event == "abandoned"


def test_abort_clears_relink_journal_with_note(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    revision = JjClient(repo).discover_review_stack().revisions[0]
    state_store = ReviewStateStore.for_repo(repo)
    journal = OperationJournal.begin(
        state_store.require_writable(),
        operation="relink",
        lock_holder=None,
        options={"pull_request_number": 1},
        resolved_scope={
            "bookmark": "review/feature",
            "change_id": revision.change_id,
            "commit_id": revision.commit_id,
            "pull_request_number": 1,
            "selected_revset": "@-",
        },
    )

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Applied abort actions" in captured.out
    assert "Relink changes which PR a change tracks" in captured.out
    assert not state_store.list_operations()
    assert read_journal(journal.path)[-1].event == "abandoned"


def test_abort_reports_stale_when_all_intents_have_gone_change_ids(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    state_store = ReviewStateStore.for_repo(repo)
    state_store.require_writable()
    # Use a change_id that doesn't exist in this repo.
    from jj_review.models.intent import SubmitIntent

    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID
        label="submit on @-",
        display_revset="@-",
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
        bookmarks={"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "review/feat-aaaa"},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "stale" in captured.out
    assert "cleanup" in captured.out


def test_abort_skips_live_pid_intent_and_warns(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    # Inject an intent with the current (live) PID to simulate a still-running submit.
    intent = SubmitIntent(
        kind="submit",
        pid=os.getpid(),
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        ordered_commit_ids=(stack.revisions[-1].commit_id,),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    exit_code = run_main(repo, config_path, "abort")
    captured = capsys.readouterr()

    # Should warn and exit 1 without retracting anything.
    assert exit_code == 1
    assert "still in progress" in captured.out
    # PR untouched, intent file still present.
    assert fake_repo.pull_requests[1].state == "open"
    assert state_store.list_intents()


def test_abort_preserves_state_and_intent_when_step_is_blocked(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # When remote branch deletion fails (e.g. network outage), abort should:
    # - still close the PR (PR close runs before local steps)
    # - preserve the state cache entry so the user retains PR tracking data
    # - keep the intent file so the user can re-run abort once the block clears
    from jj_review.jj import JjCommandError

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    intent = SubmitIntent(
        kind="submit",
        pid=99999999,  # dead PID — simulates an interrupted operation
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        ordered_commit_ids=(stack.revisions[-1].commit_id,),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    # Make remote branch deletion fail to exercise the partial-retraction path.
    from jj_review import bootstrap as bootstrap_module

    RealJjClient = bootstrap_module.JjClient

    class FailingDeleteJjClient(RealJjClient):  # type: ignore[misc]
        def delete_remote_bookmarks(self, *args, **kwargs):
            raise JjCommandError("simulated network failure")

    monkeypatch.setattr(bootstrap_module, "JjClient", FailingDeleteJjClient)

    exit_code = run_main(repo, config_path, "abort")
    capsys.readouterr()

    assert exit_code == 1
    # PR was still closed (PR close succeeds before local steps run).
    assert fake_repo.pull_requests[1].state == "closed"
    # State cache preserved — PR number and bookmark name survive for diagnosis.
    assert change_id in state_store.load().changes
    # Intent file preserved — user can re-run abort once the block clears.
    assert state_store.list_intents()


def test_abort_keeps_intent_when_recorded_remote_now_points_elsewhere(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    upstream_repo = initialize_bare_repository(
        tmp_path / "remotes-extra",
        owner="octo-org",
        name="other-review",
    )
    run_command(["jj", "git", "remote", "add", "upstream", str(upstream_repo.git_dir)], repo)

    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        ordered_commit_ids=(stack.revisions[-1].commit_id,),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    run_command(["jj", "git", "remote", "remove", "origin"], repo)
    run_command(["jj", "git", "remote", "add", "origin", str(upstream_repo.git_dir)], repo)

    def _parse_fake_repo(remote):
        if remote.name == "origin":
            return ParsedGithubRepo(
                host="github.test",
                owner="octo-org",
                repo="other-review",
            )
        return ParsedGithubRepo(
            host="github.test",
            owner=fake_repo.owner,
            repo=fake_repo.name,
        )

    monkeypatch.setattr("jj_review.commands.abort.parse_github_repo", _parse_fake_repo)

    exit_code = run_main(repo, config_path, "abort")
    capsys.readouterr()

    assert exit_code == 1
    assert fake_repo.pull_requests[1].state == "closed"
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)
    assert change_id in state_store.load().changes
    assert state_store.list_intents()


def test_abort_keeps_intent_when_recorded_remote_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = JjClient(repo).discover_review_stack()
    change_id = stack.revisions[-1].change_id
    state_store = ReviewStateStore.for_repo(repo)
    bookmark = state_store.load().changes[change_id].bookmark
    assert bookmark is not None

    intent = SubmitIntent(
        kind="submit",
        pid=99999999,
        label=f"submit on {change_id[:8]}",
        display_revset=change_id[:8],
        ordered_commit_ids=(stack.revisions[-1].commit_id,),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=(change_id,),
        bookmarks={change_id: bookmark},
        started_at="2026-01-01T00:00:00+00:00",
    )
    write_new_intent(state_store.state_dir, intent)

    run_command(["jj", "git", "remote", "remove", "origin"], repo)

    exit_code = run_main(repo, config_path, "abort")
    capsys.readouterr()

    assert exit_code == 1
    assert fake_repo.pull_requests[1].state == "closed"
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)
    assert change_id in state_store.load().changes
    assert state_store.list_intents()


def test_abort_retracts_using_recorded_remote_not_current_selection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    other_repo = initialize_bare_repository(
        tmp_path / "remotes-extra",
        owner="octo-org",
        name="other-review",
    )
    run_command(["jj", "git", "remote", "add", "upstream", str(fake_repo.git_dir)], repo)
    run_command(["jj", "git", "remote", "remove", "origin"], repo)
    run_command(["jj", "git", "remote", "add", "origin", str(other_repo.git_dir)], repo)

    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")

    stack = JjClient(repo).discover_review_stack()
    revision = stack.revisions[-1]
    change_id = revision.change_id
    bookmark = f"review/manual-{change_id[:8]}"
    run_command(["jj", "bookmark", "create", bookmark, "-r", change_id], repo)
    run_command(["jj", "git", "push", "--remote", "upstream", "--bookmark", bookmark], repo)
    fake_pull_request = fake_repo.create_pull_request(
        base_ref="main",
        body="manual body",
        head_ref=bookmark,
        title="feature 1",
    )
    fake_pull_request_url = fake_pull_request.to_payload(
        repository=fake_repo,
        web_origin="https://github.test",
    )["html_url"]

    state_store = ReviewStateStore.for_repo(repo)
    state_store.save(
        ReviewState(
            changes={
                change_id: CachedChange(
                    bookmark=bookmark,
                    pr_number=fake_pull_request.number,
                    pr_state="open",
                    pr_url=str(fake_pull_request_url),
                )
            }
        )
    )
    write_new_intent(
        state_store.state_dir,
        SubmitIntent(
            kind="submit",
            pid=99999999,
            label=f"submit on {change_id[:8]}",
            display_revset=change_id[:8],
            ordered_commit_ids=(revision.commit_id,),
            remote_name="upstream",
            github_host="github.test",
            github_owner="octo-org",
            github_repo="stacked-review",
            ordered_change_ids=(change_id,),
            bookmarks={change_id: bookmark},
            started_at="2026-01-01T00:00:00+00:00",
        ),
    )

    def _parse_repo_by_remote(remote):
        if remote.name == "origin":
            return ParsedGithubRepo(
                host="github.test",
                owner="octo-org",
                repo="other-review",
            )
        return ParsedGithubRepo(
            host="github.test",
            owner=fake_repo.owner,
            repo=fake_repo.name,
        )

    monkeypatch.setattr("jj_review.commands.abort.parse_github_repo", _parse_repo_by_remote)

    exit_code = run_main(repo, config_path, "abort")
    capsys.readouterr()

    assert exit_code == 0
    assert fake_repo.pull_requests[fake_pull_request.number].state == "closed"
    assert f"refs/heads/{bookmark}" not in remote_refs(fake_repo.git_dir)
    assert change_id not in state_store.load().changes
    assert not state_store.list_intents()
