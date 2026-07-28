"""Mutation guards for GitHub native stack resources."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.commands._github_stack_support import resolve_github_stack_support
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack
from jj_stack.state.store import ReviewStateStore


def selected_native_stack(
    *,
    selected_pull_numbers: Collection[int],
    stacks: Sequence[GithubStack],
) -> GithubStack | None:
    """Return the one native GitHub stack the selected pull requests belong to.

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
            t"The selected reviews belong to native GitHub stacks "
            t"{ui.join(lambda number: f'#{number}', numbers)}.",
            hint=t"Run {ui.join(lambda number: ui.cmd(f'gh stack unstack {number}'), numbers)}, "
            t"then retry.",
        )
    if not dissolvable and len(overlapping) > 1:
        numbers = tuple(sorted(stack.number for stack in overlapping))
        raise CliError(
            t"The selected reviews are merged members of native GitHub stacks "
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
            t"{ui.cmd(f'gh stack unstack {stack.number}')}, then retry.",
        )
    return stack


@dataclass(frozen=True, slots=True)
class GithubStackSelection:
    """Live native membership for one exact ordered PR selection."""

    github_client: GithubClient
    pull_numbers: tuple[int, ...]
    state_store: ReviewStateStore

    async def observe(
        self,
        *,
        persist: bool = True,
    ) -> tuple[bool, tuple[GithubStack, ...]]:
        """Resolve support and return current complete native resources."""

        try:
            support = await resolve_github_stack_support(
                github_client=self.github_client,
                state_store=self.state_store,
                persist=persist,
            )
            if not support.supported:
                return False, ()
            stacks = (
                support.observed_stacks
                if support.observed_stacks is not None
                else await self.github_client.list_stacks()
            )
        except GithubClientError as error:
            raise CliError("Could not inspect native GitHub stack membership.") from error
        return True, stacks

    async def active_stacks(self, *, persist: bool = True) -> tuple[GithubStack, ...]:
        """Return the resources in which a selected review is still an active member.

        Only these resources can be dissolved or blocked on: GitHub keeps merged members
        forever, so a resource the selection only touches through them needs no mutation.
        """

        if not self.pull_numbers:
            return ()
        _supported, stacks = await self.observe(persist=persist)
        selected = set(self.pull_numbers)
        return tuple(
            stack
            for stack in stacks
            if not selected.isdisjoint(stack.active_pull_request_numbers)
        )

    async def require_unstacked(
        self,
        *,
        persist: bool = True,
    ) -> None:
        """Reject an ordinary mutation while a selected review is still an active member."""

        blocking = await self.active_stacks(persist=persist)
        if not blocking:
            return
        stack_number = blocking[0].number
        raise CliError(
            t"GitHub stack #{stack_number} blocks this jj-stack operation.",
            hint=t"Run {ui.cmd(f'gh stack unstack {stack_number}')} and retry.",
        )

    async def dissolve_exact(
        self,
        *,
        observed: tuple[GithubStack, ...] | None = None,
    ) -> GithubStack | None:
        """Dissolve one exact selected resource before mutating its pull requests."""

        current = await self.recheck_active_suffix(observed=observed)
        if current is None:
            return None
        try:
            remaining = await self.github_client.unstack(stack_number=current.number)
        except GithubClientError as error:
            raise CliError(t"Could not dissolve GitHub stack #{current.number}.") from error
        if remaining is not None and (
            remaining.number != current.number
            or remaining.pull_request_numbers != current.historical_pull_request_numbers
        ):
            members = ", ".join(f"#{number}" for number in remaining.pull_request_numbers)
            raise CliError(
                t"GitHub stack #{current.number} still contains {members}.",
                hint=t"Resolve its locked pull requests, run "
                t"{ui.cmd(f'gh stack unstack {current.number}')}, then retry jj-stack.",
            )
        return current

    async def recheck_active_suffix(
        self,
        *,
        observed: tuple[GithubStack, ...] | None = None,
        persist: bool = True,
    ) -> GithubStack | None:
        """Return the current resource after confirming its active membership."""

        stacks = observed if observed is not None else await self.active_stacks(persist=persist)
        if not stacks:
            return None
        stack = selected_native_stack(selected_pull_numbers=self.pull_numbers, stacks=stacks)
        assert stack is not None
        try:
            current = await self.github_client.get_stack(stack_number=stack.number)
        except GithubClientError as error:
            raise CliError(t"Could not inspect GitHub stack #{stack.number}.") from error
        if current.pull_request_numbers != stack.pull_request_numbers:
            raise CliError(
                t"GitHub stack #{stack.number} changed before jj-stack could dissolve it.",
                hint=t"Run {ui.cmd(f'gh stack unstack {stack.number}')}, then retry.",
            )
        return current
