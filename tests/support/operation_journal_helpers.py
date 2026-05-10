from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from jj_review.state.journal import OperationJournal, SubmitOperationRecord
from jj_review.state.store import ReviewStateStore


def write_submit_operation(
    state_dir: Path,
    *,
    display_revset: str,
    ordered_change_ids: Sequence[str],
    bookmarks: Mapping[str, str] | None = None,
    github_host: str = "github.test",
    github_owner: str = "octo-org",
    github_repo: str = "stacked-review",
    ordered_commit_ids: Sequence[str] = (),
    remote_name: str = "origin",
) -> OperationJournal:
    """Write an incomplete submit operation journal for tests."""

    return OperationJournal.begin(
        state_dir,
        operation="submit",
        lock_holder=None,
        options={
            "remote_name": remote_name,
            "github_host": github_host,
            "github_owner": github_owner,
            "github_repo": github_repo,
        },
        resolved_scope={
            "bookmarks": dict(bookmarks or {}),
            "ordered_change_ids": tuple(ordered_change_ids),
            "ordered_commit_ids": tuple(ordered_commit_ids),
            "selected_revset": display_revset,
        },
    )


def incomplete_submit_operations(repo: Path) -> tuple[SubmitOperationRecord, ...]:
    """Return incomplete submit operation records for a test repo."""

    return tuple(
        loaded.operation
        for loaded in ReviewStateStore.for_repo(repo).list_operations()
        if isinstance(loaded.operation, SubmitOperationRecord)
    )
