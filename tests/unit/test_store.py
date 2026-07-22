from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from jj_stack.models.review_state import (
    LinkState,
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
)
from jj_stack.state.store import (
    ReviewStateConflictError,
    ReviewStateError,
    ReviewStateStore,
)


def _identity(*, pr_number: int = 17, link_state: LinkState = "active") -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.com",
        repository_owner="octocat",
        repository_name="example",
        pr_number=pr_number,
        head_owner="octocat",
        head_ref=f"review/change-{pr_number}",
        bookmark_ownership="managed",
        link_state=link_state,
    )


def test_review_state_store_creates_and_loads_separate_records(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-stack" / "repos" / "repo-id" / "state.json"
    store = ReviewStateStore(state_path)
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    persisted = store.create_review("change-1", identity=identity, baseline=baseline)

    assert persisted == store.load()
    assert persisted.review_identities == {"change-1": identity}
    assert persisted.submitted_baselines == {"change-1": baseline}
    assert state_path.exists()


def test_review_state_store_returns_schema_two_defaults_when_file_is_missing(
    tmp_path: Path,
) -> None:
    state = ReviewStateStore(tmp_path / "missing" / "state.json").load()

    assert state == ReviewState()
    assert state.version == 2


def test_atomic_replace_failure_preserves_original_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "state.json"
    store = ReviewStateStore(state_path)
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    original_state = store.create_review("change-1", identity=identity, baseline=baseline)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError(errno.EIO, "simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(ReviewStateError, match="simulated replace failure"):
        store.set_link_state(
            "change-1",
            expected_identity=identity,
            link_state="unlinked",
        )

    assert store.load() == original_state
    assert not tuple(tmp_path.glob("state.json.*.tmp"))


def test_stale_expected_identity_cannot_mutate_tracking(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    original_state = store.create_review("change-1", identity=identity, baseline=baseline)

    with pytest.raises(ReviewStateConflictError, match="reload and retry"):
        store.set_link_state(
            "change-1",
            expected_identity=_identity(pr_number=99),
            link_state="unlinked",
        )

    assert store.load() == original_state


def test_unrelated_write_preserves_invalid_record_as_opaque_json(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    invalid_identity = {"version": 7, "nested": ["do", {"not": "interpret"}]}
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "review_identities": {
                    "broken": invalid_identity,
                    "change-1": _identity().model_dump(mode="json"),
                },
                "submitted_baselines": {
                    "change-1": SubmittedBaseline(commit_id="abc123").model_dump(mode="json")
                },
            }
        ),
        encoding="utf-8",
    )
    store = ReviewStateStore(state_path)
    loaded = store.load()

    persisted = store.advance_baseline(
        "change-1",
        expected_identity=loaded.review_identities["change-1"],
        expected_baseline=loaded.submitted_baselines["change-1"],
        baseline=SubmittedBaseline(commit_id="def456"),
    )

    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    assert rendered["review_identities"]["broken"] == invalid_identity
    assert "broken" not in persisted.review_identities
    assert [(issue.record_type, issue.change_id) for issue in persisted.record_issues] == [
        ("review_identity", "broken")
    ]


def test_explicit_relink_replaces_only_the_exact_observed_invalid_record(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "review_identities": {"broken": {"version": 9}},
                "submitted_baselines": {},
            }
        ),
        encoding="utf-8",
    )
    store = ReviewStateStore(state_path)
    observed = store.load()
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    rendered["review_identities"]["broken"] = {"version": 8}
    state_path.write_text(json.dumps(rendered), encoding="utf-8")

    with pytest.raises(ReviewStateConflictError):
        store.relink_review(
            "broken",
            expected_identity=None,
            expected_baseline=None,
            expected_issues=observed.issues_for("broken"),
            identity=identity,
            baseline=baseline,
        )

    current = store.load()
    state = store.relink_review(
        "broken",
        expected_identity=None,
        expected_baseline=None,
        expected_issues=current.issues_for("broken"),
        identity=identity,
        baseline=baseline,
    )
    assert state.review_identities == {"broken": identity}
    assert state.submitted_baselines == {"broken": baseline}


def test_relink_distinguishes_a_missing_record_from_concurrent_json_null(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    identity = _identity()
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "review_identities": {"change-1": identity.model_dump(mode="json")},
                "submitted_baselines": {},
            }
        ),
        encoding="utf-8",
    )
    store = ReviewStateStore(state_path)
    observed = store.load()
    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    rendered["submitted_baselines"]["change-1"] = None
    state_path.write_text(json.dumps(rendered), encoding="utf-8")

    with pytest.raises(ReviewStateConflictError):
        store.relink_review(
            "change-1",
            expected_identity=identity,
            expected_baseline=None,
            expected_issues=observed.issues_for("change-1"),
            identity=identity,
            baseline=SubmittedBaseline(commit_id="abc123"),
        )


@pytest.mark.parametrize("record_map", ["review_identities", "submitted_baselines"])
def test_relink_rejects_concurrent_deletion_of_observed_malformed_record(
    tmp_path: Path,
    record_map: str,
) -> None:
    state_path = tmp_path / "state.json"
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    payload: dict[str, Any] = {
        "version": 2,
        "review_identities": {"change-1": identity.model_dump(mode="json")},
        "submitted_baselines": {"change-1": baseline.model_dump(mode="json")},
    }
    payload[record_map]["change-1"] = None
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    store = ReviewStateStore(state_path)
    observed = store.load()
    del payload[record_map]["change-1"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReviewStateConflictError):
        store.relink_review(
            "change-1",
            expected_identity=observed.review_identities.get("change-1"),
            expected_baseline=observed.submitted_baselines.get("change-1"),
            expected_issues=observed.issues_for("change-1"),
            identity=identity,
            baseline=baseline,
        )


def test_review_state_store_rejects_unsupported_top_level_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"version": 1}\n',
        encoding="utf-8",
    )

    with pytest.raises(ReviewStateError, match="unsupported version 1") as caught:
        ReviewStateStore(state_path).load()

    assert caught.value.hint is not None
    assert "mv -i" in str(caught.value.hint)
    assert "checkout --pull-request PR" in str(caught.value.hint)
    assert "relink PR CHANGE" in str(caught.value.hint)


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

    assert store.load() == ReviewState()


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
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    primary_store.create_review("change-1", identity=identity, baseline=baseline)

    assert secondary_store.load().review_identities == {"change-1": identity}
