"""Plan and apply native GitHub stack membership for submit."""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack

NativeStackAction = Literal["none", "create", "append", "replace"]


@dataclass(frozen=True, slots=True)
class NativeStackPlan:
    """One selected-stack action plus the exact resource it depends on."""

    action: NativeStackAction
    affected_stack: GithubStack | None = None


def plan_native_stack(
    *,
    desired_pull_numbers: Sequence[int | None],
    observed_stacks: Sequence[GithubStack],
    pull_numbers_requiring_base_update: Set[int],
    retiring_pull_numbers: Sequence[int] = (),
) -> NativeStackPlan:
    """Classify one selected stack without performing or scheduling mutations."""

    desired = tuple(desired_pull_numbers)
    known_desired = tuple(number for number in desired if number is not None)
    if len(set(known_desired)) != len(known_desired):
        raise CliError("Selected changes resolve to the same pull request more than once.")

    selected = set(known_desired).union(retiring_pull_numbers)
    affected = tuple(
        stack for stack in observed_stacks if not selected.isdisjoint(stack.pull_request_numbers)
    )

    if not affected:
        action: NativeStackAction = "none" if len(desired) < 2 else "create"
        return NativeStackPlan(action)
    if len(affected) != 1:
        affected_numbers = tuple(sorted(stack.number for stack in affected))
        raise CliError(
            t"Selected reviews belong to native GitHub stacks "
            t"{ui.join(lambda number: f'#{number}', affected_numbers)}.",
            hint=t"Run {
                ui.join(
                    lambda number: ui.cmd(f'gh stack unstack {number}'),
                    affected_numbers,
                )
            }, then retry.",
        )
    stack = affected[0]
    if not set(stack.pull_request_numbers).issubset(selected):
        raise CliError(
            t"GitHub stack #{stack.number} contains reviews outside the selected local stack.",
            hint=t"Run {ui.cmd(f'gh stack unstack {stack.number}')}, then retry.",
        )
    if len(desired) < 2:
        return NativeStackPlan("replace", stack)

    if set(stack.pull_request_numbers).intersection(pull_numbers_requiring_base_update):
        return NativeStackPlan("replace", stack)

    existing = stack.pull_request_numbers
    if existing == desired:
        return NativeStackPlan("none")
    if len(existing) < len(desired) and existing == desired[: len(existing)]:
        return NativeStackPlan("append", stack)
    return NativeStackPlan("replace", stack)


async def apply_native_stack_plan(
    *,
    github_client: GithubClient,
    plan: NativeStackPlan,
    pull_numbers: tuple[int, ...],
) -> None:
    if plan.action == "none":
        return
    assert plan.action != "replace"
    try:
        observed = await github_client.list_stacks()
        current = plan_native_stack(
            desired_pull_numbers=pull_numbers,
            observed_stacks=observed,
            pull_numbers_requiring_base_update=frozenset(),
        )
        if current != plan:
            raise CliError(
                "Native GitHub stack membership changed while submit was running.",
                hint=t"Rerun {ui.cmd('jj-stack submit')} with the current membership.",
            )
        if plan.action == "create":
            updated = await github_client.create_stack(pull_numbers=pull_numbers)
        else:
            assert (stack := plan.affected_stack) is not None
            delta = pull_numbers[len(stack.pull_request_numbers) :]
            updated = await github_client.append_to_stack(
                stack_number=stack.number,
                pull_numbers=delta,
            )
    except GithubClientError as error:
        raise CliError(f"Could not {plan.action} the native GitHub stack") from error
    if updated.pull_request_numbers != pull_numbers:
        raise CliError(
            "GitHub returned unexpected native stack membership after submit.",
            hint=t"Rerun {ui.cmd('jj-stack submit')} to inspect the current membership.",
        )
