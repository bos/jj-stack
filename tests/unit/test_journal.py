from __future__ import annotations

from pathlib import Path

from jj_review.models.review_state import CachedChange
from jj_review.state.journal import OperationJournal, read_journal
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
