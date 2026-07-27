from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.state.store import (
    ReviewStateConflictError,
    ReviewStateError,
    ReviewStateStore,
)

CHANGE_ID = "abcdefghijklmno"
OTHER_CHANGE_ID = "qrstuvwxyzabcde"


def _identity(
    *,
    change_id: str = CHANGE_ID,
    pr_number: int = 17,
) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.com",
        repository_owner="octocat",
        repository_name="example",
        pr_number=pr_number,
        head_owner="octocat",
        head_ref=f"review/change-{change_id[:8]}",
    )


def test_store_persists_schema_three_identity_two_and_baseline_one(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ReviewStateStore(state_path)
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")

    persisted = store.create_review(CHANGE_ID, identity=identity, baseline=baseline)

    assert persisted == store.load()
    assert persisted.review_identities == {CHANGE_ID: identity}
    assert persisted.submitted_baselines == {CHANGE_ID: baseline}
    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    assert rendered["version"] == 3
    assert rendered["review_identities"][CHANGE_ID]["version"] == 2
    assert rendered["submitted_baselines"][CHANGE_ID]["version"] == 1


def test_store_keeps_stack_support_per_github_repository(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.json")

    store.set_stacked_pull_requests("github.com/octocat/example", True)
    store.set_stacked_pull_requests("github.com/octocat/legacy", False)

    assert store.get_stacked_pull_requests("github.com/octocat/example") is True
    assert store.get_stacked_pull_requests("github.com/octocat/legacy") is False


def test_store_rejects_ambiguous_stack_support(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 3,
                "stacked_pull_requests": {"github.com/octocat/example": "true"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReviewStateError, match="valid boolean"):
        ReviewStateStore(state_path).get_stacked_pull_requests("github.com/octocat/example")


def test_store_returns_schema_three_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = ReviewStateStore(tmp_path / "missing" / "state.json").load()

    assert state == ReviewState()
    assert state.version == 3


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
            expected_identity=identity,
            expected_baseline=baseline,
            identity=ReviewIdentity(
                github_host=identity.github_host,
                repository_owner=identity.repository_owner,
                repository_name=identity.repository_name,
                pr_number=18,
                head_owner=identity.head_owner,
                head_ref="review/renamed-change-abcdefgh",
            ),
            baseline=SubmittedBaseline(commit_id="def456"),
        )

    assert store.load() == original
    assert not tuple(tmp_path.glob("state.json.*.tmp"))


def test_batch_relink_conflict_changes_no_review_pair(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    first_identity = _identity()
    second_identity = _identity(change_id=OTHER_CHANGE_ID, pr_number=18)
    old_baseline = SubmittedBaseline(commit_id="old")
    raced_baseline = SubmittedBaseline(commit_id="raced")
    store.create_review(CHANGE_ID, identity=first_identity, baseline=old_baseline)
    store.create_review(OTHER_CHANGE_ID, identity=second_identity, baseline=old_baseline)
    store.relink_review(
        OTHER_CHANGE_ID,
        expected_identity=second_identity,
        expected_baseline=old_baseline,
        identity=second_identity,
        baseline=raced_baseline,
    )

    with pytest.raises(ReviewStateConflictError):
        store.relink_reviews(
            expected={
                CHANGE_ID: (first_identity, old_baseline),
                OTHER_CHANGE_ID: (second_identity, old_baseline),
            },
            replacements={
                CHANGE_ID: (_identity(pr_number=19), SubmittedBaseline(commit_id="new-1")),
                OTHER_CHANGE_ID: (
                    _identity(change_id=OTHER_CHANGE_ID, pr_number=20),
                    SubmittedBaseline(commit_id="new-2"),
                ),
            },
        )

    state = store.load()
    assert state.review_identities[CHANGE_ID] == first_identity
    assert state.submitted_baselines[CHANGE_ID] == old_baseline
    assert state.submitted_baselines[OTHER_CHANGE_ID] == raced_baseline


def test_stale_expected_identity_cannot_retire_pair(tmp_path: Path) -> None:
    store = ReviewStateStore(tmp_path / "state.json")
    identity = _identity()
    baseline = SubmittedBaseline(commit_id="abc123")
    original = store.create_review(CHANGE_ID, identity=identity, baseline=baseline)

    with pytest.raises(ReviewStateConflictError, match="reload and retry"):
        store.retire_review(
            CHANGE_ID,
            expected_identity=_identity(pr_number=99),
            expected_baseline=baseline,
        )

    assert store.load() == original


def test_store_isolates_identity_v1_inside_schema_three(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    legacy_identity = _identity().model_dump(mode="json")
    legacy_identity["version"] = 1
    state_path.write_text(
        json.dumps(
            {
                "version": 3,
                "review_identities": {CHANGE_ID: legacy_identity},
                "submitted_baselines": {
                    CHANGE_ID: SubmittedBaseline(commit_id="abc123").model_dump(mode="json")
                },
            }
        ),
        encoding="utf-8",
    )

    state = ReviewStateStore(state_path).load()

    assert CHANGE_ID not in state.review_identities
    assert [(issue.record_type, issue.change_id) for issue in state.record_issues] == [
        ("review_identity", CHANGE_ID)
    ]


def test_store_isolates_identity_with_wrong_branch_suffix(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 3,
                "review_identities": {
                    CHANGE_ID: _identity(change_id=OTHER_CHANGE_ID).model_dump(mode="json")
                },
                "submitted_baselines": {
                    CHANGE_ID: SubmittedBaseline(commit_id="abc123").model_dump(mode="json")
                },
            }
        ),
        encoding="utf-8",
    )

    state = ReviewStateStore(state_path).load()

    assert CHANGE_ID not in state.review_identities
    assert state.issues_for(CHANGE_ID)[0].record_type == "review_identity"


def test_unrelated_write_preserves_invalid_record_as_opaque_json(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    invalid_identity = {"version": 7, "nested": ["do", {"not": "interpret"}]}
    state_path.write_text(
        json.dumps(
            {
                "version": 3,
                "review_identities": {
                    OTHER_CHANGE_ID: invalid_identity,
                    CHANGE_ID: _identity().model_dump(mode="json"),
                },
                "submitted_baselines": {
                    CHANGE_ID: SubmittedBaseline(commit_id="abc123").model_dump(mode="json")
                },
            }
        ),
        encoding="utf-8",
    )
    store = ReviewStateStore(state_path)
    loaded = store.load()

    store.relink_review(
        CHANGE_ID,
        expected_identity=loaded.review_identities[CHANGE_ID],
        expected_baseline=loaded.submitted_baselines[CHANGE_ID],
        identity=loaded.review_identities[CHANGE_ID],
        baseline=SubmittedBaseline(commit_id="def456"),
    )

    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    assert rendered["review_identities"][OTHER_CHANGE_ID] == invalid_identity


def test_relink_replaces_only_exact_observed_invalid_record(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 3,
                "review_identities": {CHANGE_ID: {"version": 9}},
                "submitted_baselines": {},
            }
        ),
        encoding="utf-8",
    )
    store = ReviewStateStore(state_path)
    observed = store.load()
    rendered = json.loads(state_path.read_text(encoding="utf-8"))
    rendered["review_identities"][CHANGE_ID] = {"version": 8}
    state_path.write_text(json.dumps(rendered), encoding="utf-8")

    with pytest.raises(ReviewStateConflictError):
        store.relink_review(
            CHANGE_ID,
            expected_identity=None,
            expected_baseline=None,
            expected_issues=observed.issues_for(CHANGE_ID),
            identity=_identity(),
            baseline=SubmittedBaseline(commit_id="abc123"),
        )

    current = store.load()
    with pytest.raises(ReviewStateConflictError):
        store.relink_review(
            CHANGE_ID,
            expected_identity=None,
            expected_baseline=None,
            expected_issues=tuple(
                issue.model_copy(update={"change_id": OTHER_CHANGE_ID})
                for issue in current.issues_for(CHANGE_ID)
            ),
            identity=_identity(),
            baseline=SubmittedBaseline(commit_id="abc123"),
        )
    repaired = store.relink_review(
        CHANGE_ID,
        expected_identity=None,
        expected_baseline=None,
        expected_issues=current.issues_for(CHANGE_ID),
        identity=_identity(),
        baseline=SubmittedBaseline(commit_id="abc123"),
    )
    assert repaired.review_identities == {CHANGE_ID: _identity()}


@pytest.mark.parametrize(
    ("observed_baselines", "concurrent_baselines"),
    (({}, {CHANGE_ID: None}), ({CHANGE_ID: None}, {})),
)
def test_relink_rejects_concurrent_transition_between_missing_and_null(
    tmp_path: Path,
    observed_baselines: dict[str, object],
    concurrent_baselines: dict[str, object],
) -> None:
    state_path = tmp_path / "state.json"
    identity = _identity()
    payload = {
        "version": 3,
        "review_identities": {CHANGE_ID: identity.model_dump(mode="json")},
        "submitted_baselines": observed_baselines,
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    store = ReviewStateStore(state_path)
    observed = store.load()
    payload["submitted_baselines"] = concurrent_baselines
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReviewStateConflictError):
        store.relink_review(
            CHANGE_ID,
            expected_identity=identity,
            expected_baseline=observed.submitted_baselines.get(CHANGE_ID),
            expected_issues=observed.issues_for(CHANGE_ID),
            identity=identity,
            baseline=SubmittedBaseline(commit_id="abc123"),
        )


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
