"""Mutation guards for GitHub native stack resources."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.commands._github_stack_support import resolve_github_stack_support
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack
from jj_stack.state.store import ReviewStateStore


@dataclass(frozen=True, slots=True)
class GithubStackSelection:
    """Live native membership for one exact ordered PR selection."""

    github_client: GithubClient
    pull_numbers: tuple[int, ...]
    state_store: ReviewStateStore

    async def overlapping(self, *, persist: bool = True) -> tuple[GithubStack, ...]:
        """Return complete native resources overlapping this selection."""

        if not self.pull_numbers:
            return ()
        try:
            support = await resolve_github_stack_support(
                github_client=self.github_client,
                state_store=self.state_store,
                persist=persist,
            )
            if not support.supported:
                return ()
            stacks = (
                support.observed_stacks
                if support.observed_stacks is not None
                else await self.github_client.list_stacks()
            )
        except GithubClientError as error:
            raise CliError("Could not inspect native GitHub stack membership.") from error
        selected = set(self.pull_numbers)
        return tuple(
            stack for stack in stacks if not selected.isdisjoint(stack.pull_request_numbers)
        )

    async def require_unstacked(
        self,
        *,
        persist: bool = True,
    ) -> None:
        """Reject an ordinary mutation when the selection overlaps a native resource."""

        stacks = await self.overlapping(persist=persist)
        if not stacks:
            return
        stack_number = stacks[0].number
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

        stacks = observed if observed is not None else await self.overlapping()
        if not stacks:
            return None
        selected = tuple(self.pull_numbers)
        stack = stacks[0]
        if len(stacks) != 1 or stack.pull_request_numbers != selected:
            raise CliError(
                "The selected pull requests do not exactly match one native GitHub stack.",
                hint="Select the complete stack before retrying unstack.",
            )
        try:
            current = await self.github_client.get_stack(stack_number=stack.number)
            if current.pull_request_numbers != selected:
                raise CliError(
                    t"GitHub stack #{stack.number} changed while unstack was preparing.",
                    hint="Inspect the current stack and retry.",
                )
            remaining = await self.github_client.unstack(stack_number=stack.number)
        except GithubClientError as error:
            raise CliError(t"Could not dissolve GitHub stack #{stack.number}.") from error
        if remaining is not None:
            members = ", ".join(f"#{number}" for number in remaining.pull_request_numbers)
            raise CliError(
                t"GitHub stack #{stack.number} still contains {members}.",
                hint=t"Resolve its locked pull requests, run "
                t"{ui.cmd(f'gh stack unstack {stack.number}')}, then retry jj-stack.",
            )
        return stack
