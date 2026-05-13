from __future__ import annotations

from pathlib import Path

import pytest

from jj_review.models.review_state import CachedChange
from jj_review.state import journal as journal_module
from jj_review.state.journal import (
    OPERATION_LOG_FILENAME,
    OperationJournal,
    read_operation_log,
)
from jj_review.state.operation_lock import acquire_operation_lock


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


def test_disabled_operation_journal_drops_events(tmp_path: Path) -> None:
    journal = OperationJournal.disabled()

    journal.append("planned_mutation", {"mutation": "dry_run"})

    assert read_operation_log(tmp_path) == ()


def test_operation_journal_does_not_fsync_audit_events_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    directory_fsyncs: list[Path] = []

    monkeypatch.setattr(journal_module.os, "fsync", fsync_calls.append)
    monkeypatch.setattr(journal_module, "_fsync_directory", directory_fsyncs.append)

    journal = OperationJournal.begin(
        tmp_path,
        operation="submit",
        options={},
        resolved_scope={},
    )
    journal.append("completed", {"ok": True})

    assert fsync_calls == []
    assert directory_fsyncs == []


def test_operation_journal_durable_append_fsyncs_new_log_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    directory_fsyncs: list[Path] = []

    monkeypatch.setattr(journal_module.os, "fsync", fsync_calls.append)
    monkeypatch.setattr(journal_module, "_fsync_directory", directory_fsyncs.append)

    journal = OperationJournal.begin(
        tmp_path,
        durable=True,
        operation="land",
        options={},
        resolved_scope={},
    )
    fsyncs_after_begin = len(fsync_calls)

    journal.append("completed", {"ok": True}, durable=True)

    assert fsyncs_after_begin >= 1
    assert len(fsync_calls) > fsyncs_after_begin
    assert directory_fsyncs == [tmp_path]
