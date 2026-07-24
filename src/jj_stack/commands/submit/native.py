"""Plan and apply native GitHub stack membership for submit."""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack


@dataclass(frozen=True, slots=True)
class NativeStackPlan:
    action: Literal["none", "create", "append", "replace"]
    affected_stack: GithubStack | None = None

    @property
    def authorization_key(self) -> tuple[str, tuple[int, tuple[int, ...]] | None]:
        stack = self.affected_stack
        return self.action, None if stack is None else (stack.number, stack.pull_request_numbers)


def plan_native_stack(
    *,
    desired: tuple[int | None, ...],
    observed_stacks: Sequence[GithubStack],
    pull_numbers_requiring_base_update: Set[int],
    retiring_pull_numbers: Sequence[int] = (),
) -> NativeStackPlan:
    known_desired = tuple(number for number in desired if number is not None)
    if len(set(known_desired)) != len(known_desired):
        raise CliError("Selected changes resolve to the same pull request more than once.")

    selected = set(known_desired).union(retiring_pull_numbers)
    affected = tuple(
        stack for stack in observed_stacks if not selected.isdisjoint(stack.pull_request_numbers)
    )

    if not affected:
        return NativeStackPlan("none" if len(desired) < 2 else "create")
    if len(affected) != 1:
        affected_numbers = tuple(sorted(stack.number for stack in affected))
        commands = ui.join(lambda number: ui.cmd(f"gh stack unstack {number}"), affected_numbers)
        raise CliError(
            t"Selected reviews belong to native GitHub stacks "
            t"{ui.join(lambda number: f'#{number}', affected_numbers)}.",
            hint=t"Run {commands}, then retry.",
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


def _membership_error(message: str) -> CliError:
    return CliError(message, hint=t"Rerun {ui.cmd('jj-stack submit')} to inspect membership.")


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
        authorized = plan_native_stack(
            desired=pull_numbers,
            observed_stacks=await github_client.list_stacks(),
            pull_numbers_requiring_base_update=frozenset(),
        )
        if authorized.authorization_key != plan.authorization_key:
            raise _membership_error("Native GitHub stack membership changed during submit.")
        stack = authorized.affected_stack
        if stack is None:
            updated = await github_client.create_stack(pull_numbers=pull_numbers)
            expected_number = updated.number
        else:
            updated = await github_client.append_to_stack(
                stack_number=stack.number,
                pull_numbers=pull_numbers[len(stack.pull_request_numbers) :],
            )
            expected_number = stack.number
        if (updated.number, updated.pull_request_numbers) != (expected_number, pull_numbers):
            raise _membership_error("GitHub returned unexpected native stack membership.")
    except GithubClientError as error:
        raise CliError(
            "Could not update the native GitHub stack",
            hint=t"Resolve GitHub's reported error, then rerun {ui.cmd('jj-stack submit')}.",
        ) from error
