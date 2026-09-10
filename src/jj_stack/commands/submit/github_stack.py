"""Plan and apply GitHub stack membership for submit."""

from collections.abc import Sequence, Set
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack, GithubStackPR
from jj_stack.stack.github_stack_safety import require_merged_prefix

type GithubStackPRSnapshot = tuple[int, str, str]


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

    def creates_stack(self, pr_count: int) -> bool:
        return self.action in ("create", "replace") and pr_count >= 2


def plan_github_stack(
    *,
    desired: tuple[int | None, ...],
    is_maximal_path: bool,
    observed_stacks: Sequence[GithubStack],
    orphaned_pr_snapshots: Set[GithubStackPRSnapshot],
    pr_numbers_requiring_base_update: Set[int],
    repo: GithubRepoAddress,
) -> GithubStackPlan:
    known_desired = tuple(number for number in desired if number is not None)
    if len(set(known_desired)) != len(known_desired):
        raise CliError("Selected changes resolve to the same pull request more than once.")

    selected = set(known_desired)
    affected = tuple(
        sorted(
            (
                require_merged_prefix(stack)
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
                t"The selected changes include only some of the pull requests in GitHub stack "
                t"#{stack.number}, together with pull requests outside it.",
                hint=t"Submit the local stack that contains the rest of GitHub stack "
                t"#{stack.number} first, then retry.",
            )
        unselected = tuple(
            pr for pr in stack.prs if not pr.is_historical and pr.number not in selected
        )
        unconfirmed = tuple(
            pr.number
            for pr in unselected
            if github_stack_pr_snapshot(pr) not in orphaned_pr_snapshots
        )
        if not is_maximal_path and unconfirmed:
            raise CliError(
                t"The selected changes stop below the top of the local stack and leave out "
                t"{ui.join(lambda number: format_pr_label(number, repo=repo), unconfirmed)} from "
                t"GitHub stack #{stack.number}.",
                hint=t"Submit the local stack that contains "
                t"{ui.join(lambda number: format_pr_label(number, repo=repo), unconfirmed)} "
                t"first, then retry.",
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


async def apply_github_stack_plan(
    *,
    github_client: GithubClient,
    plan: GithubStackPlan,
    pr_numbers: tuple[int, ...],
) -> GithubStack | None:
    """Create the desired stack or append PRs to it after any replaced stacks were dissolved."""

    try:
        if plan.action == "append":
            stack = plan.affected_stacks[0]
            return await github_client.append_to_stack(
                stack_number=stack.number,
                pr_numbers=pr_numbers[len(stack.active_pr_numbers) :],
            )
        if plan.creates_stack(len(pr_numbers)):
            return await github_client.create_stack(pr_numbers=pr_numbers)
    except GithubClientError as error:
        raise CliError("Could not update the GitHub stack") from error
    return None


def github_stack_pr_snapshot(pr: GithubStackPR) -> GithubStackPRSnapshot:
    """Return the PR number, branch, and commit GitHub reports for this stack member."""

    return pr.number, pr.head.ref, pr.head.sha


def omitted_active_stack_prs(
    *,
    desired: tuple[int | None, ...],
    observed_stacks: Sequence[GithubStack],
) -> tuple[GithubStackPR, ...]:
    """Return active unselected members of GitHub stacks touched by the selection."""

    selected = {number for number in desired if number is not None}
    return tuple(
        pr
        for stack in observed_stacks
        if not selected.isdisjoint(stack.active_pr_numbers)
        for pr in stack.prs
        if not pr.is_historical and pr.number not in selected
    )
