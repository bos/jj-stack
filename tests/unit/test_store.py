from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR, TrackingState
from jj_stack.state.store import TrackingStateError, TrackingStore

CHANGE_ID = "abcdefghijklmno"
OTHER_CHANGE_ID = "qrstuvwxyzabcde"


def _identity(
    *,
    change_id: str = CHANGE_ID,
) -> PRIdentity:
    return PRIdentity(
        pr_number=17,
        head_ref=f"jj-stack/change-{change_id[:8]}",
    )


@pytest.mark.parametrize("version", (5, 6, 7))
def test_store_migrates_released_schemas_in_memory_and_persists_on_mutation(
    tmp_path: Path,
    version: int,
) -> None:
    state_path = tmp_path / "state.json"
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    old_identity = identity.model_dump(mode="json")
    old_baseline = baseline.model_dump(mode="json")
    if version < 7:
        old_identity.update(repo_owner="octocat", repo_name="example", head_owner="octocat")
    if version == 5:
        old_identity["version"], old_baseline["version"] = 3, 1
    original = (
        json.dumps(
            {
                "version": version,
                "pr_identities": {CHANGE_ID: old_identity},
                "submitted_baselines": {CHANGE_ID: old_baseline},
            }
        )
        + "\n"
    )
    state_path.write_text(original, encoding="utf-8")
    store = TrackingStore(state_path)

    assert store.load() == TrackingState(
        prs={CHANGE_ID: TrackedPR(pr_identity=identity, submitted_baseline=baseline)}
    )
    assert state_path.read_text(encoding="utf-8") == original

    store.relink_pr(
        CHANGE_ID,
        identity=identity,
        baseline=SubmittedBaseline(commit_id="def456"),
    )

    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    assert rendered == {
        "version": 8,
        "prs": {
            CHANGE_ID: {
                "pr_identity": identity.model_dump(mode="json"),
                "submitted_baseline": {"commit_id": "def456"},
            }
        },
    }


def test_atomic_relink_failure_preserves_original_pair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = TrackingStore(tmp_path / "state.json")
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    original = store.relink_pr(CHANGE_ID, identity=identity, baseline=baseline)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError(errno.EIO, "simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(TrackingStateError, match="simulated replace failure"):
        store.relink_pr(
            CHANGE_ID,
            identity=PRIdentity(
                pr_number=18,
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
        "version": 7,
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
                CHANGE_ID: _identity().model_dump(mode="json")
                | {
                    "version": 2,
                    "repo_owner": "octocat",
                    "repo_name": "example",
                    "head_owner": "octocat",
                },
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
    rendered = '{"version": 9}\n'
    state_path.write_text(rendered, encoding="utf-8")

    with pytest.raises(TrackingStateError, match="newer than supported version 8") as caught:
        TrackingStore(state_path).load()

    assert caught.value.hint is not None
    assert "Upgrade jj-stack" in str(caught.value.hint)
    assert state_path.read_text(encoding="utf-8") == rendered


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

    primary_store.relink_pr(CHANGE_ID, identity=identity, baseline=baseline)

    assert secondary_store.load() == primary_store.load()
