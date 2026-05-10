from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jj_review.models.review_state import CachedChange
from jj_review.state.journal import (
    JOURNAL_DIRNAME,
    MIN_RETAINED_JOURNALS,
    CleanupOperationRecord,
    CleanupRebaseOperationRecord,
    CloseOperationRecord,
    LandOperationRecord,
    OperationJournal,
    append_abandoned_event,
    prune_operation_journals,
    read_journal,
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

    events = read_journal(journal.path)

    assert [event.event for event in events] == [
        "begin",
        "saved_state_update",
        "completed",
    ]
    assert events[0].data["lock_holder"]["command"] == "land"
    assert events[1].data["after"]["pr_number"] == 1
    assert events[2].data["completed_change_ids"] == ["change-1"]


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
    assert loaded.operation.kind == "relink"
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


def test_prune_operation_journals_keeps_recent_files_and_minimum_count(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    journal_dir = tmp_path / JOURNAL_DIRNAME
    journal_dir.mkdir()
    old_paths = []
    for index in range(MIN_RETAINED_JOURNALS + 5):
        path = journal_dir / f"old-{index:02d}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        timestamp = (now - timedelta(days=45, seconds=index)).timestamp()
        os.utime(path, (timestamp, timestamp))
        old_paths.append(path)
    recent_path = journal_dir / "recent.jsonl"
    recent_path.write_text("{}\n", encoding="utf-8")
    recent_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(recent_path, (recent_timestamp, recent_timestamp))

    prune_operation_journals(tmp_path, now=now)

    retained = set(journal_dir.glob("*.jsonl"))
    assert recent_path in retained
    assert len(retained) == MIN_RETAINED_JOURNALS
    assert not set(old_paths[-5:]) & retained
