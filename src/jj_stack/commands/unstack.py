"""Remove a GitHub stack without closing its pull requests.

The PRs keep their base branches and dependencies. Local changes and saved pull request links
stay in place. Submitting the same local stack again recreates the GitHub stack.

With a revset or pull request, `unstack` uses the matching local stack. Use
`--stack <number>` when the GitHub stack no longer corresponds to a single local stack.

`--local` only forgets `jj-stack`'s saved pull request links. It does not change GitHub, close
pull requests, delete PR branches, or modify local changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.cleanup.actions import CleanupAction, check_tracked_pr
from jj_stack.errors import CliError, UsageError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.error_messages import require_github_target
from jj_stack.github.resolution import resolve_github_target
from jj_stack.identifiers import ChangeId
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.github_stack_safety import (
    dissolve_github_stack,
    selected_github_stack,
)
from jj_stack.stack.pr_facts import observe_github_stacks, observe_prs
from jj_stack.stack.selected import select_stack_path
from jj_stack.stack.selection import (
    resolve_linked_change_for_pr,
)
from jj_stack.state.operation_lock import operation_lock
from jj_stack.ui import plain_text

HELP = "Remove a GitHub stack without closing its pull requests"


@dataclass(frozen=True, slots=True)
class LocalUnstackAction:
    """One saved pull request link forgotten by `unstack --local`."""

    branch: str
    change_id: ChangeId
    subject: str


@dataclass(frozen=True, slots=True)
class LocalUnstackResult:
    """Result of forgetting saved pull request links."""

    actions: tuple[LocalUnstackAction, ...]
    dry_run: bool


def unstack(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    local: bool,
    pr: str | None,
    repo: Path | None,
    revset: str | None,
    stack: int | None,
) -> int:
    """CLI entrypoint for `unstack`."""

    if stack is not None and (local or pr is not None or revset is not None):
        raise UsageError(
            "jj-stack unstack --stack cannot be combined with --local, --pull-request, "
            "or a revset."
        )
    if stack is not None and stack < 1:
        raise UsageError("jj-stack unstack --stack requires a positive GitHub stack number.")

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    command = "unstack --local" if local else "unstack"
    with operation_lock(
        context.state_store,
        command=command,
        mutating=not dry_run,
    ):
        if local:
            result = _run_local_unstack(
                context=context,
                dry_run=dry_run,
                pr=pr,
                revset=revset,
            )
            _print_local_unstack_result(result)
            return 0
        return asyncio.run(
            _run_github_unstack(
                context=context,
                dry_run=dry_run,
                pr=pr,
                revset=revset,
                stack_number=stack,
            )
        )


async def _run_github_unstack(
    *,
    context: CommandContext,
    dry_run: bool,
    pr: str | None,
    revset: str | None,
    stack_number: int | None,
) -> int:
    github_target = require_github_target(
        resolve_github_target(context.jj_client.list_git_remotes())
    )

    async with context.open_github_client(repo=github_target.repo) as github_client:
        if stack_number is not None:
            github_stack = await _get_github_stack(
                github_client=github_client,
                stack_number=stack_number,
            )
            if github_stack is None:
                console.output(t"No GitHub stack #{stack_number} was found.")
                return 0
            if not github_stack.active_pr_numbers:
                console.output(
                    t"GitHub stack #{stack_number} contains only merged PRs. "
                    t"GitHub keeps them as history; there is nothing to remove."
                )
                return 0
        else:
            state, change_ids, pr_numbers = _resolve_local_github_stack(
                context=context,
                pr=pr,
                revset=revset,
            )
            if not pr_numbers:
                console.output("No saved pull request links were found for the selected stack.")
                return 0
            selected = set(pr_numbers)
            observed = tuple(
                stack
                for stack in await observe_github_stacks(github=github_client)
                if not selected.isdisjoint(stack.active_pr_numbers)
            )
            github_stack = selected_github_stack(github_target.repo, pr_numbers, observed)
            await _check_selected_prs(
                change_ids=change_ids,
                context=context,
                github_client=github_client,
                remote_name=github_target.remote.name,
                state=state,
            )

        if github_stack is not None and not dry_run:
            await dissolve_github_stack(github_client=github_client, stack=github_stack)

    if github_stack is None:
        console.output("No GitHub stack was found for the selected pull requests.")
        return 0
    action = "Would remove" if dry_run else "Removed"
    console.output(t"{action} GitHub stack #{github_stack.number}.")
    return 0


async def _get_github_stack(
    *,
    github_client: GithubClient,
    stack_number: int,
) -> GithubStack | None:
    try:
        return await github_client.get_stack(stack_number=stack_number)
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CliError(t"Could not inspect GitHub stack #{stack_number}.") from error


def _resolve_local_github_stack(
    *,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
) -> tuple[TrackingState, tuple[ChangeId, ...], tuple[int, ...]]:
    state, stack = _resolve_local_stack(context=context, pr=pr, revset=revset)

    change_ids: list[ChangeId] = []
    pr_numbers: list[int] = []
    for change in stack.changes:
        tracked_pr = state.prs.get(change.change_id)
        if tracked_pr is None:
            continue
        change_ids.append(change.change_id)
        pr_numbers.append(tracked_pr.pr_identity.pr_number)
    return state, tuple(change_ids), tuple(pr_numbers)


async def _check_selected_prs(
    *,
    change_ids: tuple[ChangeId, ...],
    context: CommandContext,
    github_client: GithubClient,
    remote_name: str,
    state: TrackingState,
) -> None:
    try:
        observation = await observe_prs(
            change_ids=change_ids,
            context=context,
            github_client=github_client,
            include_dependents=False,
            remote_name=remote_name,
            state=state,
        )
    except GithubClientError as error:
        raise CliError("Could not inspect the selected pull requests.") from error

    for change_id in change_ids:
        state_or_blocker = check_tracked_pr(change_id=change_id, observation=observation)
        if isinstance(state_or_blocker, CleanupAction):
            raise CliError(plain_text(state_or_blocker.body))


def _run_local_unstack(
    *,
    context: CommandContext,
    dry_run: bool,
    pr: str | None,
    revset: str | None,
) -> LocalUnstackResult:
    state, stack = _resolve_local_stack(context=context, pr=pr, revset=revset)
    actions: list[LocalUnstackAction] = []
    forgotten: list[ChangeId] = []
    for change in stack.changes:
        tracked_pr = state.prs.get(change.change_id)
        if tracked_pr is None:
            continue
        forgotten.append(change.change_id)
        actions.append(
            LocalUnstackAction(
                branch=tracked_pr.pr_identity.head_ref,
                change_id=change.change_id,
                subject=change.subject,
            )
        )
    if actions and not dry_run:
        for change_id in forgotten:
            context.state_store.remove_pr(change_id)
    return LocalUnstackResult(actions=tuple(actions), dry_run=dry_run)


def _resolve_local_stack(
    *,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
) -> tuple[TrackingState, LocalStack]:
    selected_revset = _resolve_local_revset(context=context, pr=pr, revset=revset)
    state = context.state_store.load()
    with console.spinner(description="Inspecting jj stack"):
        stack = select_stack_path(
            jj_client=context.jj_client,
            revset=selected_revset,
            state=state,
        ).stack
    return state, stack


def _resolve_local_revset(
    *,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
) -> str | None:
    if pr is not None:
        resolved_revset, note = resolve_linked_change_for_pr(
            jj_client=context.jj_client,
            pr_reference=pr,
            revset=revset,
        )
        console.note(note)
        return resolved_revset
    return revset


def _print_local_unstack_result(result: LocalUnstackResult) -> None:
    if not result.actions:
        console.output("No saved pull request links were found for the selected stack.")
        return
    heading = (
        "Would forget saved pull request links:"
        if result.dry_run
        else ("Forgot saved pull request links:")
    )
    console.output(heading)
    icon = "~" if result.dry_run else "✓"
    for action in result.actions:
        change_label = t"{action.subject} ({ui.change_id(action.change_id)})"
        console.output(
            t"  {icon} forget {change_label}; leave {ui.bookmark(action.branch)} unchanged"
        )
