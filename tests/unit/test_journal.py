from __future__ import annotations

from pathlib import Path

from jj_review.models.review_state import CachedChange
from jj_review.state.journal import (
    OPERATION_LOG_FILENAME,
    CleanupOperationRecord,
    CleanupRebaseOperationRecord,
    CloseOperationRecord,
    LandOperationRecord,
    OperationJournal,
    RelinkOperationRecord,
    SubmitOperationRecord,
    append_abandoned_event,
    read_journal,
    read_operation_log,
    scan_incomplete_operation_records,
)
from jj_review.state.operation_lock import OperationLockHolder


def test_operation_journal_appends_jsonl_events(tmp_path: Path) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="land",
        lock_holder=OperationLockHolder(
            command="land",
            pid=123,
            started_at="2026-01-01T00:00:00+00:00",
        ),
        options={"dry_run": False},
        resolved_scope={"selected_revset": "@-"},
    )
    journal.append(
        "saved_state_update",
        {
            "after": CachedChange(pr_number=1, pr_state="merged"),
            "before": None,
            "change_id": "change-1",
        },
    )
    journal.append("completed", {"completed_change_ids": ("change-1",)})

    events = read_operation_log(tmp_path)

    assert [event.event for event in events] == [
        "begin",
        "saved_state_update",
        "completed",
    ]
    assert events[0].data["lock_holder"]["command"] == "land"
    assert events[1].data["after"]["pr_number"] == 1
    assert events[2].data["completed_change_ids"] == ["change-1"]
    assert not journal.path.exists()


def test_operation_journal_keeps_recovery_events_until_terminal(
    tmp_path: Path,
) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="land",
        lock_holder=None,
        options={"dry_run": False},
        resolved_scope={"selected_revset": "@-"},
    )
    journal.append(
        "planned_mutation",
        {
            "change_id": "change-1",
            "mutation": "push_trunk",
        },
    )
    journal.append(
        "saved_state_update",
        {
            "after": CachedChange(pr_number=1, pr_state="merged"),
            "before": None,
            "change_id": "change-1",
        },
    )

    recovery_events = read_journal(journal.path)

    assert [event.event for event in recovery_events] == [
        "begin",
        "saved_state_update",
    ]
    assert (tmp_path / OPERATION_LOG_FILENAME).exists()


def test_scan_incomplete_operation_records_loads_land_scope(tmp_path: Path) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="land",
        lock_holder=None,
        options={
            "bypass_readiness": False,
            "cleanup_bookmarks": True,
            "selected_pr_number": 2,
        },
        resolved_scope={
            "github_repository": "octo-org/stacked-review",
            "landed_change_ids": ("change-1",),
            "landed_commit_id": "commit-1",
            "ordered_change_ids": ("change-1", "change-2"),
            "ordered_commit_ids": ("commit-1", "commit-2"),
            "planned_change_ids": ("change-1",),
            "planned_revisions": (
                {
                    "bookmark": "review/feature-1",
                    "bookmark_managed": True,
                    "change_id": "change-1",
                    "commit_id": "commit-1",
                    "pull_request_number": 1,
                    "subject": "feature 1",
                },
            ),
            "push_trunk": True,
            "remote_name": "origin",
            "selected_revset": "@-",
            "trunk_branch": "main",
        },
    )

    [loaded] = scan_incomplete_operation_records(tmp_path)

    assert loaded.path == journal.path
    assert isinstance(loaded.operation, LandOperationRecord)
    assert loaded.operation.display_revset == "@-"
    assert loaded.operation.selected_pr_number == 2
    assert loaded.operation.ordered_change_ids == ("change-1", "change-2")
    assert loaded.operation.landed_bookmarks == {"change-1": "review/feature-1"}


def test_scan_incomplete_operation_records_loads_submit_scope(tmp_path: Path) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="submit",
        lock_holder=None,
        options={
            "github_host": "github.test",
            "github_owner": "octo-org",
            "github_repo": "stacked-review",
            "remote_name": "origin",
        },
        resolved_scope={
            "bookmarks": {"change-1": "review/feature-1"},
            "ordered_change_ids": ("change-1",),
            "ordered_commit_ids": ("commit-1",),
            "selected_revset": "@-",
        },
    )

    [loaded] = scan_incomplete_operation_records(tmp_path)

    assert loaded.path == journal.path
    assert isinstance(loaded.operation, SubmitOperationRecord)
    assert loaded.operation.display_revset == "@-"
    assert loaded.operation.remote_name == "origin"
    assert loaded.operation.bookmarks == {"change-1": "review/feature-1"}
    assert loaded.operation.change_ids() == frozenset({"change-1"})


def test_scan_incomplete_operation_records_excludes_terminal_journals(
    tmp_path: Path,
) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="land",
        lock_holder=None,
        options={
            "bypass_readiness": False,
            "cleanup_bookmarks": True,
            "selected_pr_number": None,
        },
        resolved_scope={
            "github_repository": "octo-org/stacked-review",
            "landed_change_ids": ("change-1",),
            "landed_commit_id": "commit-1",
            "ordered_change_ids": ("change-1",),
            "ordered_commit_ids": ("commit-1",),
            "planned_change_ids": ("change-1",),
            "planned_revisions": (
                {
                    "bookmark": "review/feature-1",
                    "bookmark_managed": True,
                    "change_id": "change-1",
                    "commit_id": "commit-1",
                    "pull_request_number": 1,
                    "subject": "feature 1",
                },
            ),
            "push_trunk": True,
            "remote_name": "origin",
            "selected_revset": "@-",
            "trunk_branch": "main",
        },
    )
    append_abandoned_event(journal.path, reason="test")

    assert scan_incomplete_operation_records(tmp_path) == []
    assert not journal.path.exists()
    assert read_operation_log(tmp_path)[-1].event == "abandoned"


def test_scan_incomplete_operation_records_loads_relink_scope(tmp_path: Path) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="relink",
        lock_holder=None,
        options={"pull_request_number": 1},
        resolved_scope={
            "bookmark": "review/feature-1",
            "change_id": "change-1",
            "commit_id": "commit-1",
            "pull_request_number": 1,
            "selected_revset": "@-",
        },
    )

    [loaded] = scan_incomplete_operation_records(tmp_path)

    assert loaded.path == journal.path
    assert isinstance(loaded.operation, RelinkOperationRecord)
    assert loaded.operation.change_ids() == frozenset({"change-1"})


def test_scan_incomplete_operation_records_loads_cleanup_scope(tmp_path: Path) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="cleanup",
        lock_holder=None,
        options={},
        resolved_scope={"cached_change_ids": ("change-1",)},
    )

    [loaded] = scan_incomplete_operation_records(tmp_path)

    assert loaded.path == journal.path
    assert isinstance(loaded.operation, CleanupOperationRecord)
    assert loaded.operation.change_ids() == frozenset()


def test_scan_incomplete_operation_records_loads_cleanup_rebase_scope(
    tmp_path: Path,
) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="cleanup-rebase",
        lock_holder=None,
        options={},
        resolved_scope={
            "ordered_change_ids": ("change-1", "change-2"),
            "ordered_commit_ids": ("commit-1", "commit-2"),
            "selected_revset": "@-",
        },
    )

    [loaded] = scan_incomplete_operation_records(tmp_path)

    assert loaded.path == journal.path
    assert isinstance(loaded.operation, CleanupRebaseOperationRecord)
    assert loaded.operation.display_revset == "@-"
    assert loaded.operation.ordered_change_ids == ("change-1", "change-2")
    assert loaded.operation.ordered_commit_ids == ("commit-1", "commit-2")
    assert loaded.operation.change_ids() == frozenset({"change-1", "change-2"})


def test_scan_incomplete_operation_records_loads_close_scope(
    tmp_path: Path,
) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="close",
        lock_holder=None,
        options={"cleanup": True},
        resolved_scope={
            "ordered_change_ids": ("change-1", "change-2"),
            "ordered_commit_ids": ("commit-1", "commit-2"),
            "selected_revset": "@-",
        },
    )

    [loaded] = scan_incomplete_operation_records(tmp_path)

    assert loaded.path == journal.path
    assert isinstance(loaded.operation, CloseOperationRecord)
    assert loaded.operation.display_revset == "@-"
    assert loaded.operation.cleanup is True
    assert loaded.operation.ordered_change_ids == ("change-1", "change-2")
    assert loaded.operation.ordered_commit_ids == ("commit-1", "commit-2")
