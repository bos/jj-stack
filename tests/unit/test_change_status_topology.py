from __future__ import annotations

from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.stack.change_status import (
    enumerate_orphaned_records,
    submitted_state_disagreement,
)


def _change(change_id: str, *, parents: tuple[str, ...] = ("parent-commit",)) -> LocalCommit:
    return LocalCommit(
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
    *changes: LocalCommit,
    base_parent: LocalCommit | None = None,
) -> LocalStack:
    trunk = _change("trunk-change", parents=())
    return LocalStack(
        base_parent=base_parent or trunk,
        head=changes[-1],
        changes=changes,
        selected_revset="@-",
        trunk=trunk,
    )


def _identity(
    *,
    pr_number: int = 1,
) -> PRIdentity:
    return PRIdentity(
        repo_owner="octo-org",
        repo_name="stacked-prs",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref="jj-stack/example-changeaa",
    )


def test_submitted_state_disagreement_returns_empty_when_saved_state_matches() -> None:
    a = _change("change-a")
    b = _change("change-b")
    stack = _stack(a, b)
    state = TrackingState(
        pr_identities={
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
    a = _change("change-a")
    stack = _stack(a)
    state = TrackingState(
        pr_identities={"change-a": _identity()},
        submitted_baselines={"change-a": SubmittedBaseline(commit_id="old-commit-change-a")},
    )

    assert submitted_state_disagreement(state, (stack,)) == ("change-a",)


def _orphan_record(
    *,
    pr_number: int = 42,
) -> PRIdentity:
    return _identity(pr_number=pr_number)


def test_enumerate_orphans_returns_tracked_record_with_open_pr_and_no_live_change() -> None:
    a = _change("change-live")
    stack = _stack(a)
    state = TrackingState(
        pr_identities={
            "change-live": _identity(pr_number=1),
            "change-orphan": _orphan_record(),
        },
        submitted_baselines={
            "change-live": SubmittedBaseline(commit_id="commit-change-live"),
            "change-orphan": SubmittedBaseline(commit_id="commit-change-orphan"),
        },
    )

    orphans = enumerate_orphaned_records(state, (stack,))

    assert tuple(orphan.change_id for orphan in orphans) == ("change-orphan",)


def test_submitted_state_disagreement_inspects_each_stack_independently() -> None:
    a = _change("change-a")
    b = _change("change-b")
    stack_one = _stack(a)
    stack_two = _stack(b)
    state = TrackingState(
        pr_identities={
            "change-a": _identity(pr_number=1),
            "change-b": _identity(pr_number=2),
        },
        submitted_baselines={
            "change-a": SubmittedBaseline(commit_id=a.commit_id),
            "change-b": SubmittedBaseline(commit_id="old-commit-change-b"),
        },
    )

    assert submitted_state_disagreement(state, (stack_one, stack_two)) == ("change-b",)
