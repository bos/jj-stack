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

    async def overlapping(self, *, persist: bool = True) -> tuple[GithubStack, ...]:
        """Return complete native resources overlapping this selection."""

        if not self.pull_numbers:
            return ()
        _supported, stacks = await self.observe(persist=persist)
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
        selected = set(self.pull_numbers)
        active_stacks = tuple(
            stack
            for stack in stacks
            if not selected.isdisjoint(stack.active_pull_request_numbers)
        )
        if not active_stacks:
            return
        stack_number = active_stacks[0].number
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

        current = await self.authorize_exact_active_suffix(observed=observed)
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

    async def authorize_exact_active_suffix(
        self,
        *,
        observed: tuple[GithubStack, ...] | None = None,
        persist: bool = True,
    ) -> GithubStack | None:
        """Return the freshly authorized resource for this exact active suffix."""

        stacks = (
            observed if observed is not None else await self.overlapping(persist=persist)
        )
        if not stacks:
            return None
        selected = tuple(self.pull_numbers)
        selected_set = set(selected)
        active_stacks = tuple(
            stack
            for stack in stacks
            if not selected_set.isdisjoint(stack.active_pull_request_numbers)
        )
        if not active_stacks:
            return None
        historical_pull_numbers = {
            pull_number
            for stack in stacks
            for pull_number in stack.historical_pull_request_numbers
        }
        selected_active = tuple(
            pull_number for pull_number in selected if pull_number not in historical_pull_numbers
        )
        stack = active_stacks[0]
        if len(active_stacks) != 1 or stack.active_pull_request_numbers != selected_active:
            raise CliError(
                "The selected pull requests do not exactly match one native GitHub stack's "
                "active suffix.",
                hint="Select the complete active stack suffix before retrying unstack.",
            )
        try:
            current = await self.github_client.get_stack(stack_number=stack.number)
            if current.pull_request_numbers != stack.pull_request_numbers:
                raise CliError(
                    t"GitHub stack #{stack.number} changed while unstack was preparing.",
                    hint="Inspect the current stack and retry.",
                )
            current_historical = {
                *historical_pull_numbers,
                *current.historical_pull_request_numbers,
            }
            current_selected_active = tuple(
                pull_number for pull_number in selected if pull_number not in current_historical
            )
            if current.active_pull_request_numbers != current_selected_active:
                raise CliError(
                    t"GitHub stack #{stack.number}'s active suffix changed while unstack "
                    t"was preparing.",
                    hint="Inspect the current stack and retry.",
                )
        except GithubClientError as error:
            raise CliError(t"Could not inspect GitHub stack #{stack.number}.") from error
        return current
