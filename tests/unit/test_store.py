from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.models.review_state import (
    CachedChange,
    PendingDirectLand,
    PendingDirectLandRevision,
    ReviewState,
)
from jj_stack.state.store import ReviewStateError, ReviewStateStore


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
    pending = PendingDirectLand(
        bookmark_prefix="review",
        cleanup_bookmarks=True,
        cleanup_user_bookmarks=False,
        github_host="github.test",
        github_repository="octo-org/stacked-review",
        operation_id="operation-1",
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

    store.save(ReviewState(pending_direct_land=pending), durable=True)

    assert store.load().pending_direct_land == pending


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
