from __future__ import annotations

from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.change_status import (
    enumerate_orphaned_records,
    submitted_state_disagreement,
)


def _revision(change_id: str, *, parents: tuple[str, ...] = ("parent-commit",)) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=f"commit-{change_id}",
        current_working_copy=False,
        description=f"{change_id} subject\n\nbody",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=parents,
    )


def _stack(
    *revisions: LocalRevision,
    base_parent: LocalRevision | None = None,
) -> LocalStack:
    trunk = _revision("trunk-change", parents=())
    return LocalStack(
        base_parent=base_parent or trunk,
        head=revisions[-1],
        revisions=revisions,
        selected_revset="@-",
        trunk=trunk,
    )


def _identity(
    *,
    pr_number: int = 1,
) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref="review/example-changeaa",
    )


def test_submitted_state_disagreement_returns_empty_when_saved_state_matches() -> None:
    a = _revision("change-a")
    b = _revision("change-b")
    stack = _stack(a, b)
    state = ReviewState(
        review_identities={
            "change-a": _identity(pr_number=1),
            "change-b": _identity(pr_number=2),
        },
        submitted_baselines={
            "change-a": SubmittedBaseline(commit_id=a.commit_id),
            "change-b": SubmittedBaseline(commit_id=b.commit_id),
        },
    )

    assert submitted_state_disagreement(state, (stack,)) == ()


def test_submitted_state_disagreement_flags_rewritten_commit() -> None:
    a = _revision("change-a")
    stack = _stack(a)
    state = ReviewState(
        review_identities={"change-a": _identity()},
        submitted_baselines={"change-a": SubmittedBaseline(commit_id="old-commit-change-a")},
    )

    assert submitted_state_disagreement(state, (stack,)) == ("change-a",)


def test_submitted_state_disagreement_skips_records_without_saved_baseline() -> None:
    a = _revision("change-a")
    stack = _stack(a)
    state = ReviewState(review_identities={"change-a": _identity()})

    assert submitted_state_disagreement(state, (stack,)) == ()


def _orphan_record(
    *,
    pr_number: int = 42,
) -> ReviewIdentity:
    return _identity(pr_number=pr_number)


def test_enumerate_orphans_returns_tracked_record_with_open_pr_and_no_live_change() -> None:
    a = _revision("change-live")
    stack = _stack(a)
    state = ReviewState(
        review_identities={
            "change-live": _identity(pr_number=1),
            "change-orphan": _orphan_record(),
        },
        submitted_baselines={"change-live": SubmittedBaseline(commit_id="commit-change-live")},
    )

    orphans = enumerate_orphaned_records(state, (stack,))

    assert tuple(orphan.change_id for orphan in orphans) == ("change-orphan",)


def test_enumerate_orphaned_records_reports_every_active_record_with_a_pr() -> None:
    state = ReviewState(review_identities={"change-orphan": _orphan_record()})

    orphans = enumerate_orphaned_records(state, ())

    assert tuple(orphan.change_id for orphan in orphans) == ("change-orphan",)


def test_submitted_state_disagreement_inspects_each_stack_independently() -> None:
    a = _revision("change-a")
    b = _revision("change-b")
    stack_one = _stack(a)
    stack_two = _stack(b)
    state = ReviewState(
        review_identities={
            "change-a": _identity(pr_number=1),
            "change-b": _identity(pr_number=2),
        },
        submitted_baselines={
            "change-a": SubmittedBaseline(commit_id=a.commit_id),
            "change-b": SubmittedBaseline(commit_id="old-commit-change-b"),
        },
    )

    assert submitted_state_disagreement(state, (stack_one, stack_two)) == ("change-b",)
