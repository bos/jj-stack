"""Plan and apply GitHub stack membership for submit."""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack


@dataclass(frozen=True, slots=True)
class GithubStackPlan:
    action: Literal["none", "create", "append", "replace"]
    affected_stacks: tuple[GithubStack, ...] = ()

    def __post_init__(self) -> None:
        has_stack = bool(self.affected_stacks)
        if has_stack != (self.action in ("append", "replace")):
            raise ValueError(f"Invalid GitHub stack plan: {self.action} with stack={has_stack}")
        if self.action == "append" and len(self.affected_stacks) != 1:
            raise ValueError("A GitHub stack append requires exactly one existing stack.")

    @property
    def membership_key(
        self,
    ) -> tuple[str, tuple[tuple[int, tuple[int, ...]], ...] | None]:
        stacks = tuple((stack.number, stack.pr_numbers) for stack in self.affected_stacks)
        return self.action, stacks or None


def plan_github_stack(
    *,
    desired: tuple[int | None, ...],
    is_maximal_path: bool,
    observed_stacks: Sequence[GithubStack],
    pr_numbers_requiring_base_update: Set[int],
) -> GithubStackPlan:
    known_desired = tuple(number for number in desired if number is not None)
    if len(set(known_desired)) != len(known_desired):
        raise CliError("Selected changes resolve to the same pull request more than once.")

    selected = set(known_desired)
    affected = tuple(
        sorted(
            (
                stack
                for stack in observed_stacks
                if not selected.isdisjoint(stack.active_pr_numbers)
            ),
            key=lambda stack: stack.number,
        )
    )
    partial = tuple(
        stack for stack in affected if not set(stack.active_pr_numbers).issubset(selected)
    )
    if partial:
        stack = partial[0]
        selected_outside_stack = selected.difference(stack.pr_numbers)
        if len(affected) > 1 or selected_outside_stack:
            raise CliError(
                t"The selected path includes only part of GitHub stack #{stack.number} while "
                t"also including pull requests outside that GitHub stack.",
                hint=t"Submit the other local path containing the remaining pull requests in "
                t"GitHub stack #{stack.number}, then retry.",
            )
        if not is_maximal_path:
            raise CliError(
                "The selected path stops before its local head.",
                hint="Submit the complete local path before refreshing GitHub stack membership.",
            )
        return GithubStackPlan("replace", affected)
    if not affected:
        return GithubStackPlan("none" if len(desired) < 2 else "create")
    if len(affected) > 1:
        return GithubStackPlan("replace", affected)
    stack = affected[0]
    active_pr_numbers = stack.active_pr_numbers

    if set(active_pr_numbers).intersection(pr_numbers_requiring_base_update):
        return GithubStackPlan("replace", affected)

    if active_pr_numbers == desired:
        return GithubStackPlan("none")
    if (
        len(active_pr_numbers) < len(desired)
        and active_pr_numbers == desired[: len(active_pr_numbers)]
    ):
        return GithubStackPlan("append", affected)
    return GithubStackPlan("replace", affected)


def _membership_error(message: str) -> CliError:
    return CliError(message, hint=t"Rerun {ui.cmd('jj-stack submit')} to inspect membership.")


async def apply_github_stack_plan(
    *,
    github_client: GithubClient,
    plan: GithubStackPlan,
    pr_numbers: tuple[int, ...],
) -> None:
    if plan.action == "none":
        return
    assert plan.action != "replace"
    try:
        current_plan = plan_github_stack(
            desired=pr_numbers,
            is_maximal_path=True,
            observed_stacks=await github_client.list_stacks(),
            pr_numbers_requiring_base_update=frozenset(),
        )
        if current_plan.membership_key != plan.membership_key:
            raise _membership_error("GitHub stack membership changed during submit.")
        if current_plan.action == "create":
            updated = await github_client.create_stack(pr_numbers=pr_numbers)
            expected_number = updated.number
            expected_members = pr_numbers
        elif current_plan.action == "append":
            stack = current_plan.affected_stacks[0]
            updated = await github_client.append_to_stack(
                stack_number=stack.number,
                pr_numbers=pr_numbers[len(stack.active_pr_numbers) :],
            )
            expected_number = stack.number
            expected_members = (*stack.historical_pr_numbers, *pr_numbers)
        else:
            raise AssertionError(f"Cannot apply GitHub stack plan {current_plan.action!r}.")
        if (updated.number, updated.pr_numbers) != (
            expected_number,
            expected_members,
        ):
            raise _membership_error("GitHub returned unexpected stack membership.")
    except GithubClientError as error:
        raise CliError(
            "Could not update the GitHub stack",
            hint=t"Resolve GitHub's reported error, then rerun {ui.cmd('jj-stack submit')}.",
        ) from error
