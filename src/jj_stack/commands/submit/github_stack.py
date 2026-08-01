"""Plan and apply GitHub stack membership for submit."""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.commands._github_stack_safety import selected_github_stack
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack


@dataclass(frozen=True, slots=True)
class GithubStackPlan:
    action: Literal["none", "create", "append", "replace"]
    affected_stack: GithubStack | None = None

    def __post_init__(self) -> None:
        has_stack = self.affected_stack is not None
        if has_stack != (self.action in ("append", "replace")):
            raise ValueError(f"Invalid GitHub stack plan: {self.action} with stack={has_stack}")

    @property
    def membership_key(self) -> tuple[str, tuple[int, tuple[int, ...]] | None]:
        stack = self.affected_stack
        return self.action, None if stack is None else (stack.number, stack.pull_request_numbers)


def plan_github_stack(
    *,
    desired: tuple[int | None, ...],
    observed_stacks: Sequence[GithubStack],
    pull_numbers_requiring_base_update: Set[int],
) -> GithubStackPlan:
    known_desired = tuple(number for number in desired if number is not None)
    if len(set(known_desired)) != len(known_desired):
        raise CliError("Selected changes resolve to the same pull request more than once.")

    selected = set(known_desired)
    stack = selected_github_stack(selected_pull_numbers=selected, stacks=observed_stacks)
    if stack is None or selected.isdisjoint(stack.active_pull_request_numbers):
        return GithubStackPlan("none" if len(desired) < 2 else "create")
    active_pull_numbers = stack.active_pull_request_numbers

    if set(active_pull_numbers).intersection(pull_numbers_requiring_base_update):
        return GithubStackPlan("replace", stack)

    if active_pull_numbers == desired:
        return GithubStackPlan("none")
    if (
        len(active_pull_numbers) < len(desired)
        and active_pull_numbers == desired[: len(active_pull_numbers)]
    ):
        return GithubStackPlan("append", stack)
    return GithubStackPlan("replace", stack)


def _membership_error(message: str) -> CliError:
    return CliError(message, hint=t"Rerun {ui.cmd('jj-stack submit')} to inspect membership.")


async def apply_github_stack_plan(
    *,
    github_client: GithubClient,
    plan: GithubStackPlan,
    pull_numbers: tuple[int, ...],
) -> None:
    if plan.action == "none":
        return
    assert plan.action != "replace"
    try:
        current_plan = plan_github_stack(
            desired=pull_numbers,
            observed_stacks=await github_client.list_stacks(),
            pull_numbers_requiring_base_update=frozenset(),
        )
        if current_plan.membership_key != plan.membership_key:
            raise _membership_error("GitHub stack membership changed during submit.")
        if current_plan.action == "create":
            updated = await github_client.create_stack(pull_numbers=pull_numbers)
            expected_number = updated.number
            expected_members = pull_numbers
        elif current_plan.action == "append":
            assert (stack := current_plan.affected_stack) is not None
            updated = await github_client.append_to_stack(
                stack_number=stack.number,
                pull_numbers=pull_numbers[len(stack.active_pull_request_numbers) :],
            )
            expected_number = stack.number
            expected_members = (*stack.historical_pull_request_numbers, *pull_numbers)
        else:
            raise AssertionError(f"Cannot apply GitHub stack plan {current_plan.action!r}.")
        if (updated.number, updated.pull_request_numbers) != (
            expected_number,
            expected_members,
        ):
            raise _membership_error("GitHub returned unexpected stack membership.")
    except GithubClientError as error:
        raise CliError(
            "Could not update the GitHub stack",
            hint=t"Resolve GitHub's reported error, then rerun {ui.cmd('jj-stack submit')}.",
        ) from error
