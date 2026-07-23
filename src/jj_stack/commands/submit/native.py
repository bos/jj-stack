"""Pure planning for native GitHub stack membership."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

from jj_stack.errors import CliError
from jj_stack.models.github import GithubStack

NativeStackAction = Literal["none", "create", "append", "replace"]


@dataclass(frozen=True, slots=True)
class NativeStackPlan:
    """One selected-stack action plus the exact resources it depends on."""

    action: NativeStackAction
    affected_stacks: tuple[GithubStack, ...] = ()


def plan_native_stack(
    *,
    desired_pull_numbers: Sequence[int | None],
    observed_stacks: Sequence[GithubStack],
    pull_numbers_requiring_base_update: Set[int],
) -> NativeStackPlan:
    """Classify one selected stack without performing or scheduling mutations."""

    desired = tuple(desired_pull_numbers)
    known_desired = tuple(number for number in desired if number is not None)
    if len(set(known_desired)) != len(known_desired):
        raise CliError("Selected changes resolve to the same pull request more than once.")

    selected = set(known_desired)
    selected_membership = Counter(
        number
        for stack in observed_stacks
        for number in stack.pull_request_numbers
        if number in selected
    )
    if any(count > 1 for count in selected_membership.values()):
        raise CliError("GitHub reports ambiguous native stack membership for selected reviews.")

    affected = tuple(
        sorted(
            (
                stack
                for stack in observed_stacks
                if not selected.isdisjoint(stack.pull_request_numbers)
            ),
            key=lambda stack: stack.number,
        )
    )

    if not affected:
        action: NativeStackAction = "none" if len(desired) < 2 else "create"
        return NativeStackPlan(action)
    if len(desired) < 2:
        return NativeStackPlan("replace", affected)

    affected_pull_numbers = {
        number for stack in affected for number in stack.pull_request_numbers
    }
    if affected_pull_numbers.intersection(pull_numbers_requiring_base_update):
        return NativeStackPlan("replace", affected)
    if len(affected) != 1:
        return NativeStackPlan("replace", affected)

    existing = affected[0].pull_request_numbers
    if existing == desired:
        return NativeStackPlan("none")
    if len(existing) < len(desired) and existing == desired[: len(existing)]:
        return NativeStackPlan("append", affected)
    return NativeStackPlan("replace", affected)


def shared_reviewed_change_ids(
    *,
    active_review_change_ids: Set[str],
    local_stack_change_ids: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    """Return active reviewed changes present in more than one local stack."""

    counts = Counter(
        change_id
        for stack_change_ids in local_stack_change_ids
        for change_id in set(stack_change_ids).intersection(active_review_change_ids)
    )
    return tuple(sorted(change_id for change_id, count in counts.items() if count > 1))
