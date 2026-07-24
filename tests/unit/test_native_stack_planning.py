from __future__ import annotations

import pytest

from jj_stack.commands.submit.native import plan_native_stack
from jj_stack.errors import CliError, error_hint, error_message
from jj_stack.models.github import GithubStack
from jj_stack.ui import plain_text


def _stack(
    number: int,
    *pull_numbers: int,
    historical: tuple[int, ...] = (),
    closed: tuple[int, ...] = (),
) -> GithubStack:
    return GithubStack(
        number=number,
        pull_requests=tuple(
            {
                "head": {
                    "ref": f"review/pull-{pull_number}",
                    "sha": f"head-{pull_number}",
                },
                "merged_at": ("2026-07-23T12:00:00Z" if pull_number in historical else None),
                "number": pull_number,
                "state": (
                    "closed" if pull_number in historical or pull_number in closed else "open"
                ),
            }
            for pull_number in pull_numbers
        ),
    )


@pytest.mark.parametrize(
    (
        "desired",
        "observed",
        "base_updates",
        "retiring",
        "expected_action",
        "expected_stack_number",
    ),
    (
        ((1,), (), frozenset(), (), "none", None),
        ((1, None), (_stack(5, 9, 10),), frozenset(), (), "create", None),
        ((1, 2), (_stack(7, 1, 2),), frozenset(), (), "none", None),
        ((1, 2, None, 3), (_stack(7, 1, 2),), frozenset(), (), "append", 7),
        ((1, 2, 3), (_stack(7, 1, 2),), frozenset({3}), (), "append", 7),
        ((1, 2), (_stack(7, 1, 2),), frozenset({1}), (), "replace", 7),
        ((2, 1), (_stack(7, 1, 2),), frozenset(), (), "replace", 7),
        ((None, 1, 2), (_stack(7, 1, 2),), frozenset(), (), "replace", 7),
        ((1,), (_stack(7, 1),), frozenset(), (), "replace", 7),
        ((None, None), (_stack(7, 1, 2),), frozenset(), (1, 2), "replace", 7),
        ((2,), (_stack(7, 1, 2, historical=(1,)),), frozenset(), (), "none", None),
        (
            (2, 3),
            (_stack(7, 1, 2, historical=(1,)),),
            frozenset(),
            (),
            "append",
            7,
        ),
        (
            (3, 2),
            (_stack(7, 1, 2, 3, historical=(1,)),),
            frozenset(),
            (),
            "replace",
            7,
        ),
        (
            (9, 10),
            (_stack(7, 1, 2, historical=(1, 2)),),
            frozenset(),
            (),
            "create",
            None,
        ),
    ),
)
def test_native_stack_plan_classifies_selected_membership(
    desired: tuple[int | None, ...],
    observed: tuple[GithubStack, ...],
    base_updates: frozenset[int],
    retiring: tuple[int, ...],
    expected_action: str,
    expected_stack_number: int | None,
) -> None:
    plan = plan_native_stack(
        desired=desired,
        observed_stacks=observed,
        pull_numbers_requiring_base_update=base_updates,
        retiring_pull_numbers=retiring,
    )

    assert plan.action == expected_action
    affected_number = None if plan.affected_stack is None else plan.affected_stack.number
    assert affected_number == expected_stack_number


@pytest.mark.parametrize(
    ("desired", "observed", "message_parts", "hint_parts"),
    (
        ((1, 1), (), ("same pull request",), ()),
        (
            (1, 2),
            (_stack(7, 1, 2), _stack(9, 2, 3)),
            ("#7", "#9"),
            ("gh stack unstack 7", "gh stack unstack 9"),
        ),
        (
            (1, 2),
            (_stack(7, 1, 2, 9),),
            ("#7", "outside"),
            ("gh stack unstack 7",),
        ),
        (
            (2,),
            (_stack(7, 1, 2, 9, historical=(1,), closed=(9,)),),
            ("#7", "outside"),
            ("gh stack unstack 7",),
        ),
        (
            (1, 2, 3),
            (_stack(9, 3, 4), _stack(7, 1, 2)),
            ("#7", "#9"),
            ("gh stack unstack 7", "gh stack unstack 9"),
        ),
    ),
)
def test_native_stack_plan_rejects_ambiguous_selected_membership(
    desired: tuple[int, ...],
    observed: tuple[GithubStack, ...],
    message_parts: tuple[str, ...],
    hint_parts: tuple[str, ...],
) -> None:
    with pytest.raises(CliError) as caught:
        plan_native_stack(
            desired=desired,
            observed_stacks=observed,
            pull_numbers_requiring_base_update=frozenset(),
        )

    message = plain_text(error_message(caught.value))
    hint = plain_text(error_hint(caught.value) or "")
    assert all(part in message for part in message_parts)
    assert all(part in hint for part in hint_parts)
