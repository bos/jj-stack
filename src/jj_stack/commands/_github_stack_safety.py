"""Mutation guards for GitHub stack resources."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack


def selected_github_stack(
    *,
    selected_pull_numbers: Collection[int],
    stacks: Sequence[GithubStack],
) -> GithubStack | None:
    """Return the one GitHub stack the selected pull requests belong to.

    The selection may overlap at most one resource, and every active member of that resource
    must be selected. A merged prefix GitHub retains is always valid, so a resource the selection
    touches only through merged members is still returned for callers that reconcile them.
    Callers that mutate a resource separately require a selected review to remain active.
    """

    selected = set(selected_pull_numbers)
    overlapping = tuple(
        stack for stack in stacks if not selected.isdisjoint(stack.pull_request_numbers)
    )
    if not overlapping:
        return None
    # Only a resource the selection still has an active member in can be dissolved. GitHub keeps
    # merged members forever, so unstacking a fully merged resource changes nothing, and naming
    # one in a hint would leave the user retrying a command that cannot make progress.
    dissolvable = tuple(
        stack
        for stack in overlapping
        if not selected.isdisjoint(stack.active_pull_request_numbers)
    )
    if len(dissolvable) > 1:
        numbers = tuple(sorted(stack.number for stack in dissolvable))
        raise CliError(
            t"The selected reviews belong to GitHub stacks "
            t"{ui.join(lambda number: f'#{number}', numbers)}.",
            hint=t"Run "
            t"{ui.join(lambda number: ui.cmd(f'jj-stack unstack --stack {number}'), numbers)}, "
            t"then retry.",
        )
    if not dissolvable and len(overlapping) > 1:
        numbers = tuple(sorted(stack.number for stack in overlapping))
        raise CliError(
            t"The selected reviews are merged members of GitHub stacks "
            t"{ui.join(lambda number: f'#{number}', numbers)}.",
            hint="Select changes belonging to one of those stacks, then retry.",
        )
    stack = dissolvable[0] if dissolvable else overlapping[0]
    unselected = tuple(
        number for number in stack.active_pull_request_numbers if number not in selected
    )
    if unselected:
        raise CliError(
            t"GitHub stack #{stack.number} keeps "
            t"{ui.join(lambda number: f'#{number}', unselected)} active outside the selected "
            t"stack.",
            hint=t"Select the complete stack, or run "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')}, then retry.",
        )
    return stack


async def dissolve_github_stack(
    *,
    github_client: GithubClient,
    stack: GithubStack,
) -> None:
    """Dissolve an observed stack and reject an incomplete mutation result."""

    try:
        remaining = await github_client.unstack(stack_number=stack.number)
    except GithubClientError as error:
        raise CliError(t"Could not remove GitHub stack grouping #{stack.number}.") from error
    if remaining is not None:
        members = ", ".join(f"#{number}" for number in remaining.pull_request_numbers)
        raise CliError(
            t"GitHub stack #{stack.number} still contains {members}.",
            hint=t"Resolve its locked pull requests, then retry "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')}.",
        )


@dataclass(frozen=True, slots=True)
class GithubStackSelection:
    """Live stack membership for one exact ordered PR selection."""

    github_client: GithubClient
    pull_numbers: tuple[int, ...]

    async def observe(self) -> tuple[GithubStack, ...]:
        """Return the current complete GitHub stack resources."""

        try:
            return await self.github_client.list_stacks()
        except GithubClientError as error:
            raise CliError("Could not inspect GitHub stack membership.") from error

    async def active_stacks(self) -> tuple[GithubStack, ...]:
        """Return the resources in which a selected review is still an active member.

        Only these resources can be dissolved or blocked on: GitHub keeps merged members
        forever, so a resource the selection only touches through them needs no mutation.
        """

        if not self.pull_numbers:
            return ()
        stacks = await self.observe()
        selected = set(self.pull_numbers)
        return tuple(
            stack
            for stack in stacks
            if not selected.isdisjoint(stack.active_pull_request_numbers)
        )

    async def require_unstacked(self) -> None:
        """Reject an ordinary mutation while a selected review is still an active member."""

        blocking = await self.active_stacks()
        if not blocking:
            return
        stack_number = blocking[0].number
        raise CliError(
            t"GitHub stack #{stack_number} blocks this jj-stack operation.",
            hint=t"Run {ui.cmd(f'jj-stack unstack --stack {stack_number}')} and retry.",
        )
