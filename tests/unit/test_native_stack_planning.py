from __future__ import annotations

import pytest

from jj_stack.commands.submit.native import (
    plan_native_stack,
    shared_reviewed_change_ids,
)
from jj_stack.errors import CliError
from jj_stack.models.github import GithubStack


def _stack(number: int, *pull_numbers: int) -> GithubStack:
    return GithubStack(
        number=number,
        pull_requests=tuple({"number": pull_number} for pull_number in pull_numbers),
    )


@pytest.mark.parametrize(
    (
        "desired",
        "observed",
        "base_updates",
        "expected_action",
        "expected_affected",
    ),
    (
        ((1,), (), frozenset(), "none", ()),
        ((1, None), (_stack(5, 9, 10),), frozenset(), "create", ()),
        ((1, 2), (_stack(7, 1, 2),), frozenset(), "none", ()),
        ((1, 2, None, 3), (_stack(7, 1, 2),), frozenset(), "append", (7,)),
        ((1, 2, 3), (_stack(7, 1, 2),), frozenset({3}), "append", (7,)),
        ((1, 2), (_stack(7, 1, 2),), frozenset({1}), "replace", (7,)),
        ((2, 1), (_stack(7, 1, 2),), frozenset(), "replace", (7,)),
        ((None, 1, 2), (_stack(7, 1, 2),), frozenset(), "replace", (7,)),
        ((1,), (_stack(7, 1, 2),), frozenset(), "replace", (7,)),
        ((1,), (_stack(7, 1),), frozenset(), "replace", (7,)),
        ((1, 2), (_stack(7, 1, 2, 9),), frozenset(), "replace", (7,)),
        (
            (1, 2, 3),
            (_stack(9, 3, 4), _stack(7, 1, 2)),
            frozenset(),
            "replace",
            (7, 9),
        ),
    ),
)
def test_native_stack_plan_classifies_selected_membership(
    desired: tuple[int | None, ...],
    observed: tuple[GithubStack, ...],
    base_updates: frozenset[int],
    expected_action: str,
    expected_affected: tuple[int, ...],
) -> None:
    plan = plan_native_stack(
        desired_pull_numbers=desired,
        observed_stacks=observed,
        pull_numbers_requiring_base_update=base_updates,
    )

    assert plan.action == expected_action
    assert tuple(stack.number for stack in plan.affected_stacks) == expected_affected
    assert plan.affected_stacks == tuple(
        stack
        for stack in sorted(observed, key=lambda stack: stack.number)
        if stack.number in expected_affected
    )


@pytest.mark.parametrize(
    ("desired", "observed"),
    (
        ((1, 1), ()),
        ((1, 2), (_stack(7, 1, 2), _stack(9, 2, 3))),
    ),
)
def test_native_stack_plan_rejects_ambiguous_selected_membership(
    desired: tuple[int, ...],
    observed: tuple[GithubStack, ...],
) -> None:
    with pytest.raises(CliError):
        plan_native_stack(
            desired_pull_numbers=desired,
            observed_stacks=observed,
            pull_numbers_requiring_base_update=frozenset(),
        )


@pytest.mark.parametrize(
    ("local_stacks", "active_reviews", "expected"),
    (
        ((("a", "b"), ("c",)), frozenset({"a", "b", "c"}), ()),
        ((("a", "b"), ("a", "c")), frozenset({"a", "b", "c"}), ("a",)),
        ((("a", "b"), ("a", "c")), frozenset({"b", "c"}), ()),
    ),
)
def test_shared_reviewed_changes_only_reports_active_cross_stack_membership(
    local_stacks: tuple[tuple[str, ...], ...],
    active_reviews: frozenset[str],
    expected: tuple[str, ...],
) -> None:
    assert (
        shared_reviewed_change_ids(
            active_review_change_ids=active_reviews,
            local_stack_change_ids=local_stacks,
        )
        == expected
    )
