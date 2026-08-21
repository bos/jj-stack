from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.state.store import TrackingStateError, TrackingStore

CHANGE_ID = "abcdefghijklmno"
OTHER_CHANGE_ID = "qrstuvwxyzabcde"


def _identity(
    *,
    change_id: str = CHANGE_ID,
    pr_number: int = 17,
) -> PRIdentity:
    return PRIdentity(
        repo_owner="octocat",
        repo_name="example",
        pr_number=pr_number,
        head_owner="octocat",
        head_ref=f"jj-stack/change-{change_id[:8]}",
    )


def test_store_persists_schema_six_without_nested_versions(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = TrackingStore(state_path)
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    persisted = store.create_pr(CHANGE_ID, identity=identity, baseline=baseline)

    assert persisted == store.load()
    assert persisted.pr_identities == {CHANGE_ID: identity}
    assert persisted.submitted_baselines == {CHANGE_ID: baseline}
    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    assert rendered["version"] == 6
    assert "version" not in rendered["pr_identities"][CHANGE_ID]
    assert "version" not in rendered["submitted_baselines"][CHANGE_ID]


def test_store_returns_schema_six_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = TrackingStore(tmp_path / "missing" / "state.json").load()

    assert state == TrackingState()
    assert state.version == 6


def test_store_migrates_schema_five_in_memory_and_persists_on_mutation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    schema_five = {
        "version": 5,
        "pr_identities": {
            CHANGE_ID: identity.model_dump(mode="json") | {"version": 3},
        },
        "submitted_baselines": {
            CHANGE_ID: baseline.model_dump(mode="json") | {"version": 1},
        },
    }
    original = json.dumps(schema_five) + "\n"
    state_path.write_text(original, encoding="utf-8")
    store = TrackingStore(state_path)

    assert store.load() == TrackingState(
        pr_identities={CHANGE_ID: identity},
        submitted_baselines={CHANGE_ID: baseline},
    )
    assert state_path.read_text(encoding="utf-8") == original

    store.relink_pr(
        CHANGE_ID,
        identity=identity,
        baseline=SubmittedBaseline(commit_id="def456"),
    )

    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    assert rendered["version"] == 6
    assert "version" not in rendered["pr_identities"][CHANGE_ID]
    assert "version" not in rendered["submitted_baselines"][CHANGE_ID]


def test_atomic_relink_failure_preserves_original_pair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = TrackingStore(tmp_path / "state.json")
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    original = store.create_pr(CHANGE_ID, identity=identity, baseline=baseline)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError(errno.EIO, "simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(TrackingStateError, match="simulated replace failure"):
        store.relink_pr(
            CHANGE_ID,
            identity=PRIdentity(
                repo_owner=identity.repo_owner,
                repo_name=identity.repo_name,
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
        lambda state: state["submitted_baselines"].clear(),
        lambda state: state["pr_identities"].update(
            {CHANGE_ID: _identity(change_id=OTHER_CHANGE_ID).model_dump(mode="json")}
        ),
    ),
)
def test_store_rejects_invalid_complete_file(tmp_path: Path, mutate) -> None:
    state_path = tmp_path / "state.json"
    state = {
        "version": 6,
        "pr_identities": {CHANGE_ID: _identity().model_dump(mode="json")},
        "submitted_baselines": {
            CHANGE_ID: SubmittedBaseline(commit_id="abc123").model_dump(mode="json")
        },
    }
    mutate(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(TrackingStateError, match="Invalid jj-stack data") as caught:
        TrackingStore(state_path).load()

    assert caught.value.hint is not None
    assert "mv -i" in str(caught.value.hint)


def test_store_rejects_invalid_schema_five_without_rewriting(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    rendered = json.dumps(
        {
            "version": 5,
            "pr_identities": {
                CHANGE_ID: _identity().model_dump(mode="json") | {"version": 2},
            },
            "submitted_baselines": {
                CHANGE_ID: SubmittedBaseline(commit_id="abc123").model_dump(mode="json")
                | {"version": 1},
            },
        }
    )
    state_path.write_text(rendered, encoding="utf-8")

    with pytest.raises(TrackingStateError, match="unsupported persisted tracking record version"):
        TrackingStore(state_path).load()

    assert state_path.read_text(encoding="utf-8") == rendered


def test_store_rejects_newer_schema_with_upgrade_guidance(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    rendered = '{"version": 7}\n'
    state_path.write_text(rendered, encoding="utf-8")

    with pytest.raises(TrackingStateError, match="newer than supported version 6") as caught:
        TrackingStore(state_path).load()

    assert caught.value.hint is not None
    assert "Upgrade jj-stack" in str(caught.value.hint)
    assert state_path.read_text(encoding="utf-8") == rendered


def test_require_writable_creates_missing_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-stack" / "repos" / "repo-id" / "state.json"

    writable_dir = TrackingStore(state_path).require_writable()

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
    primary_store = TrackingStore.for_repo(primary_workspace)
    secondary_store = TrackingStore.for_repo(secondary_workspace)
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    primary_store.create_pr(CHANGE_ID, identity=identity, baseline=baseline)

    assert secondary_store.load().pr_identities == {CHANGE_ID: identity}
