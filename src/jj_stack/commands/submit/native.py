"""Plan and apply native GitHub stack membership for submit."""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.commands._native_stack_safety import selected_native_stack
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
    stack = selected_native_stack(selected_pull_numbers=selected, stacks=observed_stacks)
    if stack is None or selected.isdisjoint(stack.active_pull_request_numbers):
        return NativeStackPlan("none" if len(desired) < 2 else "create")
    active_pull_numbers = stack.active_pull_request_numbers

    if set(active_pull_numbers).intersection(pull_numbers_requiring_base_update):
        return NativeStackPlan("replace", stack)

    if active_pull_numbers == desired and (len(desired) >= 2 or stack.historical_pull_requests):
        return NativeStackPlan("none")
    if len(desired) < 2:
        return NativeStackPlan("replace", stack)
    if (
        len(active_pull_numbers) < len(desired)
        and active_pull_numbers == desired[: len(active_pull_numbers)]
    ):
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
                pull_numbers=pull_numbers[len(stack.active_pull_request_numbers) :],
            )
            expected_number = stack.number
        expected_members = (
            pull_numbers
            if stack is None
            else (*stack.historical_pull_request_numbers, *pull_numbers)
        )
        if (updated.number, updated.pull_request_numbers) != (
            expected_number,
            expected_members,
        ):
            raise _membership_error("GitHub returned unexpected native stack membership.")
    except GithubClientError as error:
        raise CliError(
            "Could not update the native GitHub stack",
            hint=t"Resolve GitHub's reported error, then rerun {ui.cmd('jj-stack submit')}.",
        ) from error
