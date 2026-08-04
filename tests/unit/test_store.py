from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.state.store import ReviewStateError, ReviewStateStore

CHANGE_ID = "abcdefghijklmno"
OTHER_CHANGE_ID = "qrstuvwxyzabcde"


def _identity(
    *,
    change_id: str = CHANGE_ID,
    pr_number: int = 17,
) -> ReviewIdentity:
    return ReviewIdentity(
        repository_owner="octocat",
        repository_name="example",
        pr_number=pr_number,
        head_owner="octocat",
        head_ref=f"jj-stack/change-{change_id[:8]}",
    )


def test_store_persists_schema_four_identity_three_and_baseline_one(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ReviewStateStore(state_path)
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    persisted = store.create_review(CHANGE_ID, identity=identity, baseline=baseline)

    assert persisted == store.load()
    assert persisted.review_identities == {CHANGE_ID: identity}
    assert persisted.submitted_baselines == {CHANGE_ID: baseline}
    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    assert rendered["version"] == 4
    assert rendered["review_identities"][CHANGE_ID]["version"] == 3
    assert rendered["submitted_baselines"][CHANGE_ID]["version"] == 1


def test_store_returns_schema_four_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = ReviewStateStore(tmp_path / "missing" / "state.json").load()

    assert state == ReviewState()
    assert state.version == 4


def test_atomic_relink_failure_preserves_original_pair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    original = store.create_review(CHANGE_ID, identity=identity, baseline=baseline)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError(errno.EIO, "simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(ReviewStateError, match="simulated replace failure"):
        store.relink_review(
            CHANGE_ID,
            identity=ReviewIdentity(
                repository_owner=identity.repository_owner,
                repository_name=identity.repository_name,
                pr_number=18,
                head_owner=identity.head_owner,
                head_ref="jj-stack/renamed-change-abcdefgh",
            ),
            baseline=SubmittedBaseline(commit_id="def456"),
        )

    assert store.load() == original
    assert not tuple(tmp_path.glob("state.json.*.tmp"))


@pytest.mark.parametrize(
    "mutate",
    (
        lambda state: state["review_identities"][CHANGE_ID].pop("version"),
        lambda state: state["submitted_baselines"].clear(),
        lambda state: state["review_identities"].update(
            {CHANGE_ID: _identity(change_id=OTHER_CHANGE_ID).model_dump(mode="json")}
        ),
    ),
)
def test_store_rejects_invalid_complete_file(tmp_path: Path, mutate) -> None:
    state_path = tmp_path / "state.json"
    state = {
        "version": 4,
        "review_identities": {CHANGE_ID: _identity().model_dump(mode="json")},
        "submitted_baselines": {
            CHANGE_ID: SubmittedBaseline(commit_id="abc123").model_dump(mode="json")
        },
    }
    mutate(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ReviewStateError, match="Invalid jj-stack data") as caught:
        ReviewStateStore(state_path).load()

    assert caught.value.hint is not None
    assert "mv -i" in str(caught.value.hint)


def test_store_rejects_schema_two_without_migration(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"version": 2}\n', encoding="utf-8")

    with pytest.raises(ReviewStateError, match="unsupported version 2") as caught:
        ReviewStateStore(state_path).load()

    assert caught.value.hint is not None
    assert "mv -i" in str(caught.value.hint)
    assert "relink PR CHANGE" in str(caught.value.hint)


def test_require_writable_creates_missing_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-stack" / "repos" / "repo-id" / "state.json"

    writable_dir = ReviewStateStore(state_path).require_writable()

    assert writable_dir == state_path.parent
    assert writable_dir.exists()


def test_store_shares_tracking_across_workspaces_for_same_repo(
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
    (secondary_jj_dir / "repo").write_text(
        os.path.relpath(repo_storage, secondary_jj_dir),
        encoding="utf-8",
    )
    primary_store = ReviewStateStore.for_repo(primary_workspace)
    secondary_store = ReviewStateStore.for_repo(secondary_workspace)
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    primary_store.create_review(CHANGE_ID, identity=identity, baseline=baseline)

    assert secondary_store.load().review_identities == {CHANGE_ID: identity}
