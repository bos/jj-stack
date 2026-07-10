from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from jj_stack.models.review_state import (
    CachedChange,
    PendingDirectLand,
    PendingDirectLandRevision,
    ReviewState,
)
from jj_stack.state.store import ReviewStateError, ReviewStateStore


def _pending_direct_land() -> PendingDirectLand:
    return PendingDirectLand(
        bookmark_prefix="review",
        cleanup_bookmarks=True,
        cleanup_user_bookmarks=False,
        github_host="github.test",
        github_repository="octo-org/stacked-review",
        operation_id="operation-1",
        original_local_trunk_commit_id="trunk-1",
        original_trunk_commit_id="trunk-1",
        planned_revisions=(
            PendingDirectLandRevision(
                bookmark="review/feature-aaaaaaaa",
                bookmark_ownership="managed",
                change_id="change-1",
                commit_id="commit-1",
                pull_request_number=1,
                subject="feature",
            ),
        ),
        remote_name="origin",
        remote_url="https://github.test/octo-org/stacked-review.git",
        trunk_branch="main",
    )


def test_review_state_store_round_trips_and_creates_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-stack" / "repos" / "repo-id" / "state.json"
    store = ReviewStateStore(state_path)

    store.save(
        ReviewState(
            changes={
                "zvlywqkxtmnpqrstu": CachedChange(
                    bookmark="review/fix-cache-invalidation-zvlywqkx",
                    pr_review_decision="approved",
                    pr_state="open",
                )
            }
        )
    )

    loaded_state = store.load()

    assert loaded_state.changes["zvlywqkxtmnpqrstu"].bookmark == (
        "review/fix-cache-invalidation-zvlywqkx"
    )
    assert loaded_state.changes["zvlywqkxtmnpqrstu"].pr_review_decision == "approved"
    assert loaded_state.changes["zvlywqkxtmnpqrstu"].pr_state == "open"
    assert state_path.exists()


def test_review_state_store_returns_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = ReviewStateStore(tmp_path / "missing" / "state.json").load()

    assert state.version == 1
    assert state.changes == {}


def test_review_state_store_round_trips_pending_direct_land(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    pending = _pending_direct_land()

    store.save(ReviewState(pending_direct_land=pending))

    assert store.load().pending_direct_land == pending


def test_pending_direct_land_automatically_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    real_fsync = os.fsync
    fsynced_kinds: list[str] = []

    def recording_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsynced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    store.save(ReviewState(pending_direct_land=_pending_direct_land()))

    assert fsynced_kinds == ["file", "directory"]


def test_durable_save_reports_file_fsync_failure(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    store = ReviewStateStore(state_path)
    real_fsync = os.fsync

    def fail_file_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "simulated file fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_file_fsync)

    with pytest.raises(ReviewStateError, match="simulated file fsync failure"):
        store.save(ReviewState(pending_direct_land=_pending_direct_land()))

    assert not state_path.exists()
    assert not tuple(tmp_path.glob("state.json.*.tmp"))


def test_durable_save_reports_atomic_replace_failure(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    store = ReviewStateStore(state_path)
    original_state = ReviewState(changes={"old": CachedChange(bookmark="review/old")})
    store.save(original_state)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError(errno.EIO, "simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(ReviewStateError, match="simulated replace failure"):
        store.save(ReviewState(pending_direct_land=_pending_direct_land()))

    assert store.load() == original_state
    assert not tuple(tmp_path.glob("state.json.*.tmp"))


def test_durable_save_reports_directory_fsync_failure(tmp_path: Path, monkeypatch) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    real_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "simulated directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    with pytest.raises(ReviewStateError, match="simulated directory fsync failure"):
        store.save(ReviewState(pending_direct_land=_pending_direct_land()))


def test_durable_save_tolerates_unsupported_directory_fsync(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    real_fsync = os.fsync

    def reject_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory fsync is unsupported")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", reject_directory_fsync)

    store.save(ReviewState(pending_direct_land=_pending_direct_land()))

    assert store.load().pending_direct_land == _pending_direct_land()


def test_review_state_store_rejects_unknown_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        (
            "{\n"
            '  "version": 1,\n'
            '  "changes": {\n'
            '    "zvlywqkxtmnpqrstu": {\n'
            '      "potato_shape": "round"\n'
            "    }\n"
            "  }\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReviewStateError, match="potato_shape"):
        ReviewStateStore(state_path).load()


def test_require_writable_creates_missing_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-stack" / "repos" / "repo-id" / "state.json"
    store = ReviewStateStore(state_path)

    writable_dir = store.require_writable()

    assert writable_dir == state_path.parent
    assert writable_dir.exists()


def test_review_state_store_for_repo_does_not_depend_on_config_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".jj" / "repo").mkdir(parents=True)

    store = ReviewStateStore.for_repo(repo)
    store.save(ReviewState())
    loaded_state = store.load()

    assert loaded_state == ReviewState()
