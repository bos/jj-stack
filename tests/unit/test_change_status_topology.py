from __future__ import annotations

from jj_stack.models.review_state import CachedChange, LinkState, ReviewState
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


def _tracked(
    *,
    commit_id: str | None = None,
    pr_number: int = 1,
) -> CachedChange:
    return CachedChange(
        bookmark="review/example",
        last_submitted_commit_id=commit_id,
        pr_number=pr_number,
        pr_state="open",
        pr_url="https://example.test/pull/1",
    )


def test_submitted_state_disagreement_returns_empty_when_saved_state_matches() -> None:
    a = _revision("change-a")
    b = _revision("change-b")
    stack = _stack(a, b)
    state = ReviewState(
        changes={
            "change-a": _tracked(commit_id=a.commit_id, pr_number=1),
            "change-b": _tracked(commit_id=b.commit_id, pr_number=2),
        }
    )

    assert submitted_state_disagreement(state, (stack,)) == ()


def test_submitted_state_disagreement_flags_rewritten_commit() -> None:
    a = _revision("change-a")
    stack = _stack(a)
    state = ReviewState(
        changes={
            "change-a": _tracked(commit_id="old-commit-change-a"),
        }
    )

    assert submitted_state_disagreement(state, (stack,)) == ("change-a",)


def test_submitted_state_disagreement_skips_records_without_saved_baseline() -> None:
    a = _revision("change-a")
    stack = _stack(a)
    state = ReviewState(
        changes={
            "change-a": CachedChange(
                bookmark="review/example",
                pr_number=1,
                pr_state="open",
                pr_url="https://example.test/pull/1",
            )
        }
    )

    assert submitted_state_disagreement(state, (stack,)) == ()


def test_submitted_state_disagreement_skips_unlinked_records_even_when_stale() -> None:
    a = _revision("change-a")
    stack = _stack(a)
    state = ReviewState(
        changes={
            "change-a": CachedChange(
                bookmark="review/example",
                last_submitted_commit_id="old-commit-change-a",
                link_state="unlinked",
            )
        }
    )

    assert submitted_state_disagreement(state, (stack,)) == ()


def _orphan_record(
    *,
    pr_number: int | None = 42,
    pr_state: str | None,
    link_state: LinkState = "active",
    bookmark: str | None = "review/example",
) -> CachedChange:
    return CachedChange(
        bookmark=bookmark,
        link_state=link_state,
        pr_number=pr_number,
        pr_state=pr_state,
        pr_url="https://example.test/pull/42",
    )


def test_enumerate_orphans_returns_tracked_record_with_open_pr_and_no_live_change() -> None:
    a = _revision("change-live")
    stack = _stack(a)
    state = ReviewState(
        changes={
            "change-live": _tracked(commit_id="commit-change-live", pr_number=1),
            "change-orphan": _orphan_record(pr_state="open"),
        }
    )

    orphans = enumerate_orphaned_records(state, (stack,))

    assert tuple(orphan.change_id for orphan in orphans) == ("change-orphan",)


def test_enumerate_orphaned_records_treats_unknown_pr_state_as_still_open() -> None:
    state = ReviewState(
        changes={"change-orphan": _orphan_record(pr_state=None)}
    )

    orphans = enumerate_orphaned_records(state, ())

    assert tuple(orphan.change_id for orphan in orphans) == ("change-orphan",)


def test_enumerate_orphaned_records_skips_records_without_pr_number() -> None:
    state = ReviewState(
        changes={
            "change-open": _orphan_record(pr_number=None, pr_state="open"),
            "change-unknown": _orphan_record(pr_number=None, pr_state=None),
        }
    )

    assert enumerate_orphaned_records(state, ()) == ()


def test_enumerate_orphaned_records_skips_records_with_closed_or_merged_pr() -> None:
    state = ReviewState(
        changes={
            "change-closed": _orphan_record(pr_state="closed"),
            "change-merged": _orphan_record(pr_state="merged"),
        }
    )

    assert enumerate_orphaned_records(state, ()) == ()


def test_enumerate_orphaned_records_skips_unlinked_records() -> None:
    state = ReviewState(
        changes={
            "change-detached": _orphan_record(pr_state="open", link_state="unlinked"),
        }
    )

    assert enumerate_orphaned_records(state, ()) == ()


def test_submitted_state_disagreement_inspects_each_stack_independently() -> None:
    a = _revision("change-a")
    b = _revision("change-b")
    stack_one = _stack(a)
    stack_two = _stack(b)
    state = ReviewState(
        changes={
            "change-a": _tracked(commit_id=a.commit_id, pr_number=1),
            "change-b": _tracked(commit_id="old-commit-change-b", pr_number=2),
        }
    )

    assert submitted_state_disagreement(state, (stack_one, stack_two)) == ("change-b",)
