"""Ask GitHub to merge pull requests at the bottom of a stack.

Starting at the bottom of the stack, `jj-stack` selects consecutive open, non-draft pull
requests. Each must still match the commit that was last submitted; GitHub decides whether
reviews, checks, conflicts, and repo rules allow the merge.

For a direct merge, one that GitHub performs immediately rather than through a merge queue, the
command waits for GitHub to finish. It then fetches trunk, removes the merged changes from the
local stack, rebases any remaining changes onto the updated trunk, and updates their existing
pull requests.

When the trunk branch uses a merge queue, the command adds the pull requests to the queue and
exits once GitHub accepts them. It does not wait for them to merge or update the local stack.
After GitHub finishes, run `jj-stack sync <head-change-id>`.

For a direct merge, `--method` chooses among the merge methods the repo allows. Without it, the
command uses `jj-stack.merge_method` from your jj config, or the repo's only allowed method, and
otherwise prefers rebase, then squash, then a merge commit. If several methods are allowed and the
stack contains signed commits, choose one explicitly: merging can discard signatures. A merge
queue chooses its own method and ignores `--method`.

Common examples:

- `jj-stack merge --dry-run` previews the merge without changing GitHub.

- `jj-stack merge` asks GitHub to merge the ready PRs at the bottom of the stack.

- `jj-stack merge --pull-request 123 --method squash` selects PR 123 as the last PR to merge and
  chooses the merge method explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.sync import run_stack_convergence
from jj_stack.config import MergeMethod
from jj_stack.errors import CliError, error_hint
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import GithubTarget, resolve_trunk_branch
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubRepo
from jj_stack.models.stack import LocalCommit
from jj_stack.stack.pr_facts import observe_github_stacks, observe_prs
from jj_stack.stack.preparation import prepare_local_stack
from jj_stack.stack.selection import (
    resolve_linked_change_for_pr,
)
from jj_stack.state.operation_lock import operation_lock_if_mutating

from .github_stack import build_async_merge_plan, execute_async_merge
from .models import MergeExecutionInputs, MergeResult, PreparedMerge
from .plan import build_merge_plan
from .render import print_merge_result

HELP = "Merge pull requests at the bottom of a stack"


def merge(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    merge_method: str | None,
    pr: str | None,
    repo: Path | None,
    revset: str | None,
) -> int:
    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    with operation_lock_if_mutating(
        context.state_store,
        command="merge",
        mutating=not dry_run,
    ):
        return _run_merge(
            context=context,
            dry_run=dry_run,
            merge_method=merge_method,
            pr=pr,
            revset=revset,
        )


def _run_merge(
    *,
    context: CommandContext,
    dry_run: bool,
    merge_method: str | None,
    pr: str | None,
    revset: str | None,
) -> int:
    selected_revset, target_change_id = _resolve_merge_target(
        context=context,
        pr=pr,
        revset=revset,
    )
    with console.spinner(description="Inspecting jj stack"):
        prepared_merge = _prepare_merge(
            context=context,
            dry_run=dry_run,
            merge_method=merge_method,
            revset=selected_revset,
            target_change_id=target_change_id,
        )
    result = asyncio.run(_stream_merge_async(prepared_merge=prepared_merge))
    print_merge_result(result)
    if result.blocked:
        return 1
    if result.enqueued or not result.applied:
        return 0
    sync_change_id = prepared_merge.stack.head.change_id
    console.output("Updating the local stack after the completed merge:")
    try:
        exit_code = run_stack_convergence(
            context=context,
            dry_run=False,
            fetch_remote_state=True,
            revset=sync_change_id,
        )
    except BaseException as error:
        _warn_incomplete_post_merge_sync(
            sync_change_id, has_recovery_hint=error_hint(error) is not None
        )
        if isinstance(error, GithubClientError):
            raise CliError(
                "Could not update the local stack after the completed merge.",
                hint=t"Resolve the GitHub error, then run "
                t"{ui.cmd('jj-stack sync')} {ui.change_id(sync_change_id)}",
            ) from error
        raise
    if exit_code:
        _warn_incomplete_post_merge_sync(sync_change_id, has_recovery_hint=True)
    return exit_code


def _warn_incomplete_post_merge_sync(
    sync_change_id: str, *, has_recovery_hint: bool = False
) -> None:
    console.warning(
        (
            t"GitHub completed the merge, but the follow-up work did not finish. Do not run "
            t"{ui.cmd('jj-stack merge')} again.",
            (
                t" Continue with {ui.cmd('jj-stack sync')} {ui.change_id(sync_change_id)}."
                if not has_recovery_hint
                else ""
            ),
        )
    )


def _resolve_merge_target(
    *,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
) -> tuple[str | None, str | None]:
    if pr is not None:
        pr_number, resolved_revset, repo = resolve_linked_change_for_pr(
            jj_client=context.jj_client,
            pr_reference=pr,
            revset=revset,
        )
        console.note(
            t"Using {format_pr_label(pr_number, repo=repo)} for change "
            t"{ui.change_id(resolved_revset)}"
        )
        return None, resolved_revset
    return revset, None


def _prepare_merge(
    *,
    context: CommandContext,
    dry_run: bool,
    merge_method: str | None,
    revset: str | None,
    target_change_id: str | None,
) -> PreparedMerge:
    prepared = prepare_local_stack(
        containing_change_id=target_change_id,
        context=context,
        fetch_remote_state=True,
        revset=revset,
    )
    target = prepared.github_target
    if target.remote is None:
        message = target.remote_error or t"Could not determine which Git remote to use."
        raise CliError(
            message,
            hint=t"Configure one GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
    if not isinstance(target, GithubTarget):
        message = target.github_repo_error or t"Could not resolve GitHub target."
        raise CliError(
            message,
            hint=t"Point jj-stack at a GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )

    if not dry_run:
        context.state_store.require_writable()
    return PreparedMerge(
        context=context,
        dry_run=dry_run,
        merge_method=merge_method,
        stack=prepared.stack,
        state=prepared.state,
        target=target,
        target_change_id=target_change_id,
    )


async def _observe_merge_queue(github: GithubClient, branch: str) -> bool:
    try:
        return await github.base_branch_uses_merge_queue(branch=branch)
    except GithubClientError:
        return False


async def _stream_merge_async(
    *,
    prepared_merge: PreparedMerge,
) -> MergeResult:
    stack = prepared_merge.stack
    github_repo = prepared_merge.target.repo
    remote = prepared_merge.target.remote

    async with build_github_client(repo=github_repo) as github_client:
        with console.spinner(description="Inspecting remotes"):
            try:
                github_repo_state = await github_client.get_repo()
            except GithubClientError as error:
                raise CliError(
                    t"Could not inspect GitHub repo {github_repo.full_name}",
                    hint="Resolve the GitHub error above, then rerun jj-stack merge.",
                ) from error
            trunk_branch, _trunk_targets = resolve_trunk_branch(
                branches_at_trunk=prepared_merge.context.jj_client.remote_bookmarks_at_commit(
                    remote=remote.name,
                    commit_id=stack.trunk.commit_id,
                ),
                github_repo_state=github_repo_state,
                remote=remote,
                trunk_commit_id=stack.trunk.commit_id,
            )
        queue_task = asyncio.create_task(_observe_merge_queue(github_client, trunk_branch))
        prs_task = asyncio.create_task(
            observe_prs(
                change_ids=tuple(change.change_id for change in stack.changes),
                context=prepared_merge.context,
                github_client=github_client,
                github_repo_snapshot=github_repo_state,
                remote_name=remote.name,
            )
        )
        stacks_task = asyncio.create_task(observe_github_stacks(github=github_client))
        await asyncio.gather(queue_task, prs_task, stacks_task, return_exceptions=True)
        uses_merge_queue = await queue_task
        if uses_merge_queue:
            if prepared_merge.merge_method is not None:
                console.warning(
                    t"The base branch {ui.bookmark(trunk_branch)} uses a merge queue; ignoring "
                    t"{ui.cmd('--method')}."
                )
            merge_action = "merge_queue"
            resolved_merge_method = None
        else:
            merge_action = "direct_merge"
            resolved_merge_method = _resolve_merge_method(
                changes=stack.changes,
                configured=prepared_merge.context.config.merge_method,
                merge_method=prepared_merge.merge_method,
                repo_state=github_repo_state,
            )
        try:
            observation = await prs_task
        except GithubClientError as error:
            raise CliError(
                "Could not inspect GitHub state for merge.",
                hint="Resolve the GitHub error above, then rerun jj-stack merge.",
            ) from error
        plan = build_merge_plan(
            observation=observation,
            remote_name=remote.name,
            repo=github_repo,
            changes=stack.changes,
            state=prepared_merge.state,
            target_change_id=prepared_merge.target_change_id,
            trunk_branch=trunk_branch,
        )
        stacks = await stacks_task
        execution = MergeExecutionInputs(
            repo=github_client.repo,
            selected_revset=stack.selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=stack.trunk.subject,
        )
        async_merge = build_async_merge_plan(plan, stacks, execution)
        if prepared_merge.dry_run:
            if async_merge.planned:
                action = async_merge.action(
                    merge_action=merge_action,
                    method=resolved_merge_method,
                    repo=execution.repo,
                    trunk_branch=trunk_branch,
                )
                actions = (
                    (action, async_merge.boundary_action)
                    if async_merge.boundary_action is not None
                    else (action,)
                )
                return execution.result(actions=actions)
            return execution.result(
                actions=(
                    () if async_merge.boundary_action is None else (async_merge.boundary_action,)
                )
            )
        return await execute_async_merge(
            execution=execution,
            github=github_client,
            merge_action=merge_action,
            merge_method=resolved_merge_method,
            merge=async_merge,
        )


def _resolve_merge_method(
    *,
    changes: Sequence[LocalCommit],
    configured: MergeMethod | None,
    merge_method: str | None,
    repo_state: GithubRepo,
) -> str:
    """Honor explicit choices; require one for signed stacks with several allowed methods."""

    settings = {
        "rebase": repo_state.allow_rebase_merge,
        "squash": repo_state.allow_squash_merge,
        "merge": repo_state.allow_merge_commit,
    }
    chosen = merge_method or configured
    if any(allowed is None for allowed in settings.values()):
        if chosen is not None:
            return chosen
        raise CliError(
            "GitHub did not report which merge methods this repo allows.",
            hint=t"Pass {ui.cmd('--method')} or set {ui.code('jj-stack.merge_method')}.",
        )
    allowed_methods = [method for method, allowed in settings.items() if allowed]
    if not allowed_methods:
        raise CliError(
            "This repo does not allow any pull request merge method.",
            hint="Fix the repo merge settings on GitHub before merging.",
        )
    if chosen is not None:
        if chosen not in allowed_methods:
            source = ui.cmd("--method") if merge_method else ui.code("jj-stack.merge_method")
            raise CliError(
                t"This repo does not allow {ui.cmd(chosen)} merges; it allows "
                t"{ui.join(ui.cmd, allowed_methods)}.",
                hint=t"Change {source} to one it allows, or enable {ui.cmd(chosen)} on GitHub.",
            )
        return chosen
    if len(allowed_methods) == 1:
        return allowed_methods[0]
    signed = tuple(change.change_id for change in changes if change.signed)
    if signed:
        raise CliError(
            t"Stack contains signed commits: {ui.join(ui.change_id, signed)}.",
            hint=t"Choose a merge method with {ui.cmd('--method')} or "
            t"{ui.code('jj-stack.merge_method')}.",
        )
    return allowed_methods[0]
