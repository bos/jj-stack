from __future__ import annotations

from pathlib import Path

import pytest

from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.state.store import ReviewStateError, ReviewStateStore


def test_review_state_store_round_trips_and_creates_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-review" / "repos" / "repo-id" / "state.json"
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
    state_path = tmp_path / "state" / "jj-review" / "repos" / "repo-id" / "state.json"
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
