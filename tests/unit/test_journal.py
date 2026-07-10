from __future__ import annotations

from pathlib import Path

from jj_stack.models.review_state import CachedChange
from jj_stack.state.journal import (
    OPERATION_LOG_FILENAME,
    OperationJournal,
    read_operation_log,
)
from jj_stack.state.operation_lock import acquire_operation_lock


def test_operation_journal_appends_jsonl_events(tmp_path: Path) -> None:
    with acquire_operation_lock(tmp_path, command="land"):
        journal = OperationJournal.begin(
            tmp_path,
            operation="land",
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
    assert journal.path == tmp_path / OPERATION_LOG_FILENAME


def test_operation_journal_uses_one_repo_log(tmp_path: Path) -> None:
    first = OperationJournal.begin(
        tmp_path,
        operation="submit",
        options={},
        resolved_scope={"selected_revset": "@-"},
    )
    second = OperationJournal.begin(
        tmp_path,
        operation="cleanup",
        options={},
        resolved_scope={},
    )
    first.append("completed", {"ok": True})
    second.append("completed", {"ok": True})

    assert first.path == second.path == tmp_path / OPERATION_LOG_FILENAME
    assert [event.operation for event in read_operation_log(tmp_path)] == [
        "submit",
        "cleanup",
        "submit",
        "cleanup",
    ]


def test_operation_journal_records_saved_state_updates(tmp_path: Path) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="cleanup",
        options={},
        resolved_scope={},
    )
    unchanged = CachedChange(pr_number=2, pr_state="open")

    journal.record_saved_state_updates(
        before={
            "change-a": CachedChange(pr_number=1, pr_state="open"),
            "change-b": unchanged,
            "change-c": CachedChange(pr_number=3, pr_state="closed"),
        },
        after={
            "change-a": CachedChange(pr_number=1, pr_state="closed"),
            "change-b": unchanged,
            "change-d": CachedChange(pr_number=4, pr_state="open"),
        },
    )

    events = [
        event for event in read_operation_log(tmp_path) if event.event == "saved_state_update"
    ]

    assert [event.data["change_id"] for event in events] == [
        "change-a",
        "change-c",
        "change-d",
    ]
    assert events[0].data["before"]["pr_state"] == "open"
    assert events[0].data["after"]["pr_state"] == "closed"
    assert events[1].data["before"]["pr_number"] == 3
    assert events[1].data["after"] is None
    assert events[2].data["before"] is None
    assert events[2].data["after"]["pr_number"] == 4


def test_disabled_operation_journal_drops_events(tmp_path: Path) -> None:
    journal = OperationJournal.disabled()

    journal.append("planned_mutation", {"mutation": "dry_run"})

    assert read_operation_log(tmp_path) == ()


def test_operation_journal_ignores_torn_trailing_append(tmp_path: Path) -> None:
    journal = OperationJournal.begin(
        tmp_path,
        operation="land",
        options={},
        resolved_scope={},
    )
    if journal.path is None:
        raise AssertionError("Expected an enabled operation journal.")
    with journal.path.open("a", encoding="utf-8") as output:
        output.write('{"event":"completed"')
    journal.append("completed", {"completed_change_ids": ("change-1",)})

    events = read_operation_log(tmp_path)

    assert [event.event for event in events] == ["begin", "completed"]
