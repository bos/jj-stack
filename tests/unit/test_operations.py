"""Unit tests for interrupted-operation matching helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jj_review.review.operations import (
    match_cleanup_rebase_operation,
    match_close_operation,
    match_ordered_change_ids,
)
from jj_review.state.journal import CleanupRebaseOperationRecord, CloseOperationRecord
from jj_review.system import pid_is_alive

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def test_match_cleanup_rebase_operation_returns_same_logical_for_rewritten_stack() -> None:
    assert (
        match_cleanup_rebase_operation(
            operation=_make_cleanup_rebase_operation(),
            current_change_ids=("aaaa", "bbbb"),
            current_commit_ids=("new-aaaa", "new-bbbb"),
        )
        == "same-logical"
    )


def test_match_cleanup_rebase_operation_returns_same_logical_for_reordered_stack() -> None:
    assert (
        match_cleanup_rebase_operation(
            operation=_make_cleanup_rebase_operation(("aaaa", "bbbb")),
            current_change_ids=("bbbb", "aaaa"),
            current_commit_ids=("commit-bbbb", "commit-aaaa"),
        )
        == "same-logical"
    )


def test_match_cleanup_rebase_operation_returns_trimmed_for_shrunk_current_stack() -> None:
    assert (
        match_cleanup_rebase_operation(
            operation=_make_cleanup_rebase_operation(("aaaa", "bbbb", "cccc")),
            current_change_ids=("bbbb", "cccc"),
            current_commit_ids=("commit-bbbb", "commit-cccc"),
        )
        == "trimmed"
    )


def test_match_close_operation_returns_disjoint_when_cleanup_mode_differs() -> None:
    assert (
        match_close_operation(
            operation=_make_close_operation(cleanup=True),
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

