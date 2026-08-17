"""Separate a GitHub stack without closing its pull requests.

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
from jj_stack.commands._cleanup_actions import check_tracked_pr
from jj_stack.errors import CliError, UsageError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.resolution import GithubTarget, resolve_github_target
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.github_stack_safety import (
    GithubStackSelection,
    dissolve_github_stack,
    selected_github_stack,
)
from jj_stack.stack.pr_facts import observe_prs
from jj_stack.stack.selected import select_stack_path
from jj_stack.stack.selection import (
    resolve_linked_change_for_pr,
    resolve_selected_revset,
)
from jj_stack.state.operation_lock import acquire_operation_lock
from jj_stack.ui import plain_text

HELP = "Separate a GitHub stack while leaving its pull requests open"


@dataclass(frozen=True, slots=True)
class LocalUnstackAction:
    """One saved pull request link forgotten by `unstack --local`."""

    branch: str
    change_id: str
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
            "unstack --stack cannot be combined with --local, --pull-request, or a revset."
        )
    if stack is not None and stack < 1:
        raise UsageError("unstack --stack requires a positive GitHub stack number.")

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    command = "unstack --local" if local else "unstack"
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command=command,
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
    github_target = resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(github_target, GithubTarget):
        for message in github_target_unavailable_messages(github_target):
            console.warning(message)
        return 1

    async with build_github_client(repo=github_target.repo) as github_client:
        if stack_number is not None:
            github_stack = await _get_github_stack(
                github_client=github_client,
                stack_number=stack_number,
            )
            if github_stack is None:
                console.output(t"No GitHub stack grouping #{stack_number} was found.")
                return 0
        else:
            state, change_ids, pr_numbers = _resolve_local_github_stack(
                context=context,
                pr=pr,
                revset=revset,
            )
            if not pr_numbers:
                console.output("No saved pull requests were found for the selected stack.")
                return 0
            await _check_selected_prs(
                change_ids=change_ids,
                context=context,
                github_client=github_client,
                remote_name=github_target.remote.name,
                state=state,
            )
            selection = GithubStackSelection(github_client, pr_numbers)
            observed = await selection.active_stacks()
            github_stack = selected_github_stack(
                selected_pr_numbers=pr_numbers,
                stacks=observed,
            )

        if github_stack is not None and not dry_run:
            await dissolve_github_stack(github_client=github_client, stack=github_stack)

    if github_stack is None:
        console.output("No GitHub stack grouping was found for the selected pull requests.")
        return 0
    action = "Would remove" if dry_run else "Removed"
    console.output(t"{action} GitHub stack grouping #{github_stack.number}.")
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
) -> tuple[TrackingState, tuple[str, ...], tuple[int, ...]]:
    selected_revset = _resolve_local_revset(
        action_name="unstack",
        context=context,
        pr=pr,
        revset=revset,
    )
    state = context.state_store.load()
    with console.spinner(description="Inspecting jj stack"):
        stack = select_stack_path(
            jj_client=context.jj_client,
            revset=selected_revset,
            state=state,
        ).stack

    change_ids: list[str] = []
    pr_numbers: list[int] = []
    for change in stack.changes:
        tracked_pr = state.tracked_pr(change.change_id)
        if tracked_pr is None:
            continue
        change_ids.append(change.change_id)
        pr_numbers.append(tracked_pr.pr_identity.pr_number)
    return state, tuple(change_ids), tuple(pr_numbers)


async def _check_selected_prs(
    *,
    change_ids: tuple[str, ...],
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
            include_open_dependents=False,
            remote_name=remote_name,
        )
    except GithubClientError as error:
        raise CliError("Could not inspect the selected pull requests.") from error

    for change_id in change_ids:
        candidate = state.tracked_pr(change_id)
        assert candidate is not None
        _pr, blocker = check_tracked_pr(
            allowed_states=frozenset({"open", "closed", "merged"}),
            candidate=candidate,
            observation=observation,
        )
        if blocker is not None:
            raise CliError(plain_text(blocker.body))


def _run_local_unstack(
    *,
    context: CommandContext,
    dry_run: bool,
    pr: str | None,
    revset: str | None,
) -> LocalUnstackResult:
    selected_revset = _resolve_local_revset(
        action_name="unstack --local",
        context=context,
        pr=pr,
        revset=revset,
    )
    state = context.state_store.load()
    with console.spinner(description="Inspecting jj stack"):
        stack = select_stack_path(
            jj_client=context.jj_client,
            revset=selected_revset,
            state=state,
        ).stack
    actions: list[LocalUnstackAction] = []
    forgotten: list[str] = []
    for change in stack.changes:
        tracked_pr = state.tracked_pr(change.change_id)
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
            context.state_store.retire_pr(change_id)
    return LocalUnstackResult(actions=tuple(actions), dry_run=dry_run)


def _resolve_local_revset(
    *,
    action_name: str,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
) -> str | None:
    if pr is not None:
        pr_number, resolved_revset = resolve_linked_change_for_pr(
            jj_client=context.jj_client,
            pr_reference=pr,
            revset=revset,
        )
        console.note(t"Using PR #{pr_number} -> {ui.revset(resolved_revset)}")
        return resolved_revset
    return resolve_selected_revset(
        command_label=action_name,
        default_revset=None,
        require_explicit=False,
        revset=revset,
    )


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
