from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.state.store import ReviewStateError, ReviewStateStore


def test_review_state_store_round_trips_and_creates_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-stack" / "repos" / "repo-id" / "state.json"
    store = ReviewStateStore(state_path)

    store.save(
        ReviewState(
            changes={
                "zvlywqkxtmnpqrstu": CachedChange(
                    bookmark="review/fix-cache-invalidation-zvlywqkx",
                )
            }
        )
    )

    loaded_state = store.load()

    assert loaded_state.changes["zvlywqkxtmnpqrstu"].bookmark == (
        "review/fix-cache-invalidation-zvlywqkx"
    )
    assert state_path.exists()


def test_review_state_store_returns_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = ReviewStateStore(tmp_path / "missing" / "state.json").load()

    assert state.version == 1
    assert state.changes == {}


def test_save_reports_atomic_replace_failure_and_preserves_original(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    store = ReviewStateStore(state_path)
    original_state = ReviewState(changes={"old": CachedChange(bookmark="review/old")})
    store.save(original_state)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError(errno.EIO, "simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(ReviewStateError, match="simulated replace failure"):
        store.save(ReviewState(changes={"new": CachedChange(bookmark="review/new")}))

    assert store.load() == original_state
    assert not tuple(tmp_path.glob("state.json.*.tmp"))


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


def test_review_state_store_shares_tracking_across_workspaces_for_same_repo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    primary_workspace = tmp_path / "primary"
    repo_storage = primary_workspace / ".jj" / "repo"
    repo_storage.mkdir(parents=True)
    secondary_workspace = tmp_path / "secondary"
    secondary_jj_dir = secondary_workspace / ".jj"
    secondary_jj_dir.mkdir(parents=True)
    repo_pointer = os.path.relpath(repo_storage, secondary_jj_dir)
    (secondary_jj_dir / "repo").write_text(repo_pointer, encoding="utf-8")
    primary_store = ReviewStateStore.for_repo(primary_workspace)
    secondary_store = ReviewStateStore.for_repo(secondary_workspace)
    state = ReviewState(changes={"change-1": CachedChange(bookmark="review/change-1")})

    primary_store.save(state)

    assert secondary_store.load() == state
