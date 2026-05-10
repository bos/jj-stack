from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jj_review.review.submit_recovery import (
    ArtifactPresence,
    SubmitArtifactObservation,
    SubmitRecoveryIdentity,
    SubmitStatusDecision,
    SubmitTargetRelation,
    recorded_submit_still_exists_exactly,
    should_retire_submit_after_cleanup,
    should_retire_submit_after_submit,
    submit_artifacts_still_live,
    submit_status_decision,
)
from jj_review.state.journal import SubmitOperationRecord


def _make_submit_operation(
    *,
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    ordered_commit_ids: tuple[str, ...] = ("commit-aaaa", "commit-bbbb"),
    remote_name: str = "origin",
    github_repo: str = "stacked-review",
    bookmarks: dict[str, str] | None = None,
) -> SubmitOperationRecord:
    return SubmitOperationRecord(
        kind="submit",
        path=Path("submit.jsonl"),
        pid=12345,
        label="submit on @",
        display_revset="@",
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        remote_name=remote_name,
        github_host="github.test",
        github_owner="octo-org",
        github_repo=github_repo,
        bookmarks=bookmarks
        if bookmarks is not None
        else {change_id: f"review/{change_id}" for change_id in ordered_change_ids},
        started_at="2026-01-01T00:00:00+00:00",
    )


def test_submit_status_decision_is_continue_only_for_exact_matching_target() -> None:
    intent = _make_submit_operation()
    identity = SubmitRecoveryIdentity.from_operation(intent)

    assert (
        submit_status_decision(
            intent=intent,
            current_change_ids=intent.ordered_change_ids,
            current_commit_ids=intent.ordered_commit_ids,
            current_identity=identity,
        )
        is SubmitStatusDecision.CONTINUE
    )
    assert (
        submit_status_decision(
            intent=intent,
            current_change_ids=intent.ordered_change_ids,
            current_commit_ids=intent.ordered_commit_ids,
            current_identity=SubmitRecoveryIdentity(
                remote_name="origin",
                github_host="github.test",
                github_owner="octo-org",
                github_repo="other-review",
            ),
        )
        is SubmitStatusDecision.INSPECT
    )
    assert (
        submit_status_decision(
            intent=intent,
            current_change_ids=intent.ordered_change_ids,
            current_commit_ids=intent.ordered_commit_ids,
            current_identity=None,
        )
        is SubmitStatusDecision.INSPECT
    )
    assert (
        submit_status_decision(
            intent=intent,
            current_change_ids=intent.ordered_change_ids,
            current_commit_ids=("new-aaaa", "new-bbbb"),
            current_identity=identity,
        )
        is SubmitStatusDecision.CURRENT_STACK
    )
    assert (
        submit_status_decision(
            intent=intent,
            current_change_ids=("cccc",),
            current_commit_ids=("commit-cccc",),
            current_identity=identity,
        )
        is SubmitStatusDecision.OUTSTANDING
    )


def test_recorded_submit_still_exists_exactly_requires_full_exact_snapshot() -> None:
    intent = _make_submit_operation()

    assert recorded_submit_still_exists_exactly(
        intent=intent,
        commit_ids_by_change_id={"aaaa": "commit-aaaa", "bbbb": "commit-bbbb"},
    )
    assert not recorded_submit_still_exists_exactly(
        intent=intent,
        commit_ids_by_change_id={"aaaa": "commit-aaaa", "bbbb": "new-bbbb"},
    )
    assert not recorded_submit_still_exists_exactly(
        intent=intent,
        commit_ids_by_change_id={"aaaa": "commit-aaaa"},
    )


def test_should_retire_submit_after_submit_requires_matching_identity_and_bookmarks() -> None:
    old = _make_submit_operation(bookmarks={"aaaa": "review/a", "bbbb": "review/b"})
    new = _make_submit_operation(
        ordered_change_ids=("aaaa", "bbbb", "cccc"),
        ordered_commit_ids=("commit-aaaa", "commit-bbbb", "commit-cccc"),
        bookmarks={
            "aaaa": "review/a",
            "bbbb": "review/b",
            "cccc": "review/c",
        },
    )

    assert should_retire_submit_after_submit(old_intent=old, new_intent=new)
    assert not should_retire_submit_after_submit(
        old_intent=old,
        new_intent=replace(new, remote_name="upstream"),
    )
    assert not should_retire_submit_after_submit(
        old_intent=old,
        new_intent=replace(new, github_repo="other-review"),
    )
    assert not should_retire_submit_after_submit(
        old_intent=old,
        new_intent=replace(new, bookmarks={"aaaa": "review/a"}),
    )


def test_submit_artifacts_still_live_fails_closed_when_target_is_not_match() -> None:
    observation = SubmitArtifactObservation(
        target_relation=SubmitTargetRelation.UNKNOWN,
        saved_state=ArtifactPresence.ABSENT,
        local_bookmarks=ArtifactPresence.ABSENT,
        remote_bookmarks=ArtifactPresence.ABSENT,
    )

    assert submit_artifacts_still_live(observation)
    assert not should_retire_submit_after_cleanup(observation=observation)


def test_submit_artifacts_still_live_requires_all_recorded_artifacts_to_be_absent() -> None:
    assert submit_artifacts_still_live(
        SubmitArtifactObservation(
            target_relation=SubmitTargetRelation.MATCH,
            saved_state=ArtifactPresence.PRESENT,
            local_bookmarks=ArtifactPresence.ABSENT,
            remote_bookmarks=ArtifactPresence.ABSENT,
        )
    )
    assert submit_artifacts_still_live(
        SubmitArtifactObservation(
            target_relation=SubmitTargetRelation.MATCH,
            saved_state=ArtifactPresence.ABSENT,
            local_bookmarks=ArtifactPresence.PRESENT,
            remote_bookmarks=ArtifactPresence.ABSENT,
        )
    )
    assert submit_artifacts_still_live(
        SubmitArtifactObservation(
            target_relation=SubmitTargetRelation.MATCH,
            saved_state=ArtifactPresence.ABSENT,
            local_bookmarks=ArtifactPresence.ABSENT,
            remote_bookmarks=ArtifactPresence.PRESENT,
        )
    )

    cleared = SubmitArtifactObservation(
        target_relation=SubmitTargetRelation.MATCH,
        saved_state=ArtifactPresence.ABSENT,
        local_bookmarks=ArtifactPresence.ABSENT,
        remote_bookmarks=ArtifactPresence.ABSENT,
    )
    assert not submit_artifacts_still_live(cleared)
    assert should_retire_submit_after_cleanup(observation=cleared)

    unknown_remote = SubmitArtifactObservation(
        target_relation=SubmitTargetRelation.MATCH,
        saved_state=ArtifactPresence.ABSENT,
        local_bookmarks=ArtifactPresence.ABSENT,
        remote_bookmarks=ArtifactPresence.UNKNOWN,
    )
    assert submit_artifacts_still_live(unknown_remote)
    assert not should_retire_submit_after_cleanup(observation=unknown_remote)
