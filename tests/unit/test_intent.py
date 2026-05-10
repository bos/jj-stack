"""Unit tests for the intent file module."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jj_review.models.intent import SubmitIntent
from jj_review.review.intents import (
    intent_is_stale,
    match_cleanup_rebase_intent,
    match_close_intent,
    match_ordered_change_ids,
)
from jj_review.state.intents import (
    check_same_kind_intent,
    scan_intents,
    write_new_intent,
)
from jj_review.state.journal import CleanupRebaseOperationRecord, CloseOperationRecord
from jj_review.system import pid_is_alive

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_submit_intent(
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    pid: int = 12345,
) -> SubmitIntent:
    return SubmitIntent(
        kind="submit",
        pid=pid,
        label="submit on @",
        display_revset="@",
        ordered_commit_ids=("commit-aaaa", "commit-bbbb"),
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=ordered_change_ids,
        bookmarks={"aaaa": "review/feat-1-aaaa", "bbbb": "review/feat-2-bbbb"},
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_cleanup_rebase_operation(
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    pid: int = 12345,
) -> CleanupRebaseOperationRecord:
    return CleanupRebaseOperationRecord(
        kind="cleanup-rebase",
        path=Path("cleanup-rebase.jsonl"),
        pid=pid,
        label="cleanup --rebase on @",
        display_revset="@",
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=("commit-aaaa", "commit-bbbb"),
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_close_operation(
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    cleanup: bool = False,
    pid: int = 12345,
) -> CloseOperationRecord:
    return CloseOperationRecord(
        kind="close",
        path=Path("close.jsonl"),
        pid=pid,
        label="close on @",
        display_revset="@",
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=("commit-aaaa", "commit-bbbb"),
        cleanup=cleanup,
        started_at="2026-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intent_factory", "test_id"),
    [
        (_make_submit_intent, "submit"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_write_intent_round_trips_supported_intent_kinds(
    tmp_path: Path,
    intent_factory,
    test_id: str,
) -> None:
    del test_id
    intent = intent_factory()
    path = write_new_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].path == path
    assert results[0].intent == intent


def test_scan_intents_ignores_unparseable_files(tmp_path: Path) -> None:
    bad = tmp_path / "incomplete-2026-01-15-10-30.01.json"
    bad.write_text('{"not valid json"', encoding="utf-8")
    results = scan_intents(tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_match_ordered_change_ids_returns_exact_for_identical_sequences() -> None:
    assert match_ordered_change_ids(("a", "b"), ("a", "b")) == "exact"


def test_match_ordered_change_ids_returns_superset_for_extended_prefix() -> None:
    # new is a superset: existing is a prefix of new
    assert match_ordered_change_ids(("a", "b"), ("a", "b", "c")) == "superset"


def test_match_ordered_change_ids_returns_overlap_for_partial_overlap() -> None:
    # Shares some IDs but neither is prefix of the other
    assert match_ordered_change_ids(("a", "b"), ("b", "c")) == "overlap"


def test_match_ordered_change_ids_returns_overlap_for_reordered_sequences() -> None:
    # Same IDs but reordered
    assert match_ordered_change_ids(("a", "b"), ("b", "a")) == "overlap"


def test_match_ordered_change_ids_returns_disjoint_for_non_overlapping_sequences() -> None:
    assert match_ordered_change_ids(("a", "b"), ("c", "d")) == "disjoint"


def test_match_ordered_change_ids_requires_prefix_order_for_superset() -> None:
    # ["a","b"] vs ["b","a","c"] — b appears first in new but old starts with a
    assert match_ordered_change_ids(("a", "b"), ("b", "a", "c")) == "overlap"


def test_match_cleanup_rebase_intent_returns_same_logical_for_rewritten_stack() -> None:
    assert (
        match_cleanup_rebase_intent(
            intent=_make_cleanup_rebase_operation(),
            current_change_ids=("aaaa", "bbbb"),
            current_commit_ids=("new-aaaa", "new-bbbb"),
        )
        == "same-logical"
    )


def test_match_cleanup_rebase_intent_returns_same_logical_for_reordered_stack() -> None:
    assert (
        match_cleanup_rebase_intent(
            intent=_make_cleanup_rebase_operation(("aaaa", "bbbb")),
            current_change_ids=("bbbb", "aaaa"),
            current_commit_ids=("commit-bbbb", "commit-aaaa"),
        )
        == "same-logical"
    )


def test_match_cleanup_rebase_intent_returns_trimmed_for_shrunk_current_stack() -> None:
    assert (
        match_cleanup_rebase_intent(
            intent=_make_cleanup_rebase_operation(("aaaa", "bbbb", "cccc")),
            current_change_ids=("bbbb", "cccc"),
            current_commit_ids=("commit-bbbb", "commit-cccc"),
        )
        == "trimmed"
    )


def test_match_close_intent_returns_disjoint_when_cleanup_mode_differs() -> None:
    assert (
        match_close_intent(
            intent=_make_close_operation(cleanup=True),
            current_change_ids=("aaaa", "bbbb"),
            current_commit_ids=("commit-aaaa", "commit-bbbb"),
            current_cleanup=False,
        )
        == "disjoint"
    )


# ---------------------------------------------------------------------------
# PID liveness
# ---------------------------------------------------------------------------


def test_pid_is_alive_returns_true_for_current_process() -> None:
    assert pid_is_alive(os.getpid()) is True


def test_pid_is_alive_returns_false_for_missing_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", fake_kill)
    assert pid_is_alive(99999999) is False


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------


def test_stack_intent_is_stale_when_no_change_ids_still_resolve(tmp_path: Path) -> None:
    intent = _make_submit_intent(("aaaa", "bbbb"))
    assert intent_is_stale(intent, lambda cid: False) is True


def test_stack_intent_stays_live_when_any_change_id_still_resolves(tmp_path: Path) -> None:
    intent = _make_submit_intent(("aaaa", "bbbb"))
    assert intent_is_stale(intent, lambda cid: cid == "aaaa") is False


# ---------------------------------------------------------------------------
# check_same_kind_intent
# ---------------------------------------------------------------------------


def test_check_same_kind_intent_returns_stale_dead_pid_intents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.state.intents.pid_is_alive", lambda pid: False)
    old_intent = _make_submit_intent(("aaaa", "bbbb"), pid=99999999)
    write_new_intent(tmp_path, old_intent)

    new_intent = _make_submit_intent(("aaaa", "bbbb"))
    result = check_same_kind_intent(tmp_path, new_intent)

    assert len(result) == 1
    assert result[0].intent == old_intent


def test_check_same_kind_intent_reports_live_same_kind_intent_without_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []
    monkeypatch.setattr("jj_review.state.intents.pid_is_alive", lambda pid: True)
    old_intent = _make_submit_intent(("aaaa", "bbbb"), pid=99999999)
    write_new_intent(tmp_path, old_intent)

    new_intent = _make_submit_intent(("cccc", "dddd"))
    result = check_same_kind_intent(tmp_path, new_intent, print_fn=messages.append)

    assert result == []
    assert messages == ["Another submit on @ is in progress (PID 99999999)."]
