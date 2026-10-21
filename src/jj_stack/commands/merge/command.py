"""Ask GitHub to merge pull requests at the bottom of a stack.

Starting at the bottom of the stack, `jj-stack` selects consecutive open, non-draft pull
requests. Each must match the commit last submitted. It waits for required checks and stops on
the first failure; GitHub decides whether the merge is allowed.

By default, it waits for GitHub to merge, including through a queue, then fetches trunk, removes
merged local changes, rebases remaining changes onto trunk, and updates their PRs.

Use `--no-wait` to skip the check wait and return once GitHub accepts the request. Ctrl-C during
the check wait stops before requesting a merge; afterward it stops waiting without cancelling.
After GitHub finishes, run `jj-stack sync <head-change-id>`.

For a direct merge, one that GitHub performs without a merge queue, `--method` chooses among the
merge methods the repo allows. Without it, the command uses `jj-stack.merge_method` from your jj
config, or the repo's only allowed method, and otherwise prefers rebase, then squash, then a
merge commit. If several methods are allowed and the stack contains signed commits, choose one
explicitly: merging can discard signatures. A merge queue chooses its own method and ignores
`--method`.

Common examples:

- `jj-stack merge --dry-run` previews the merge without changing GitHub.

- `jj-stack merge` asks GitHub to merge the ready PRs at the bottom of the stack.

- `jj-stack merge --pull-request 123 --method squash` selects PR 123 as the last PR to merge and
  chooses the merge method explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, GlobalOptions, bootstrap_context
from jj_stack.commands.cleanup.command import cleanup_stack_without_local_copies
from jj_stack.commands.github_run import GithubRun, ObservedTrunk
from jj_stack.commands.sync import converge_selected_stack
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.config import MergeMethod
from jj_stack.errors import CliError, error_hint
from jj_stack.formatting import pr_url
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.error_messages import (
    observe_github_repo,
    read_or_stop,
    require_github_target,
)
from jj_stack.github.resolution import GithubTarget, resolve_github_target
from jj_stack.identifiers import ChangeId, short_change_id
from jj_stack.models.github import GithubRepo
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.pr_facts import observe_github_stacks, observe_prs
from jj_stack.stack.preparation import prepare_local_stack
from jj_stack.stack.selection import (
    resolve_linked_change_for_pr,
)
from jj_stack.stack.trunk import observe_trunk_branch
from jj_stack.state.operation_lock import OperationLockBusyError, operation_lock

from .checks import ChecksPending, inspect_required_checks
from .github_stack import (
    PendingMerge,
    build_async_merge_plan,
    execute_async_merge,
    prepare_merge_request,
)
from .plan import MergeChange, MergeExecutionInputs, MergeResult, build_merge_plan
from .wait import retry_while_github_catches_up

_RERUN_HINT = "Resolve the GitHub error above, then rerun jj-stack merge."
HELP = "Merge pull requests at the bottom of a stack"


@dataclass(frozen=True, slots=True)
class MergeOptions:
    """Fixed command options shared by preparation, check waiting, and completion."""

    dry_run: bool
    merge_method: MergeMethod | None
    no_wait: bool
    pr: str | None
    revset: str | None


@dataclass(frozen=True, slots=True)
class PreparedMerge:
    """Locally prepared merge inputs before GitHub planning and execution."""

    context: CommandContext
    options: MergeOptions
    stack: LocalStack
    state: TrackingState
    target: GithubTarget
    target_change_id: ChangeId | None

    @property
    def sync_head(self) -> str:
        """Short change ID of the stack head that sync uses after the merge."""

        return short_change_id(self.stack.head.change_id)


def merge(
    *,
    global_options: GlobalOptions,
    dry_run: bool,
    merge_method: MergeMethod | None,
    no_wait: bool,
    pr: str | None,
    revset: str | None,
) -> int:
    context = bootstrap_context(global_options)
    return asyncio.run(
        _run_merge(
            context=context,
            options=MergeOptions(
                dry_run=dry_run, merge_method=merge_method, no_wait=no_wait, pr=pr, revset=revset
            ),
        )
    )


async def _run_merge(
    *,
    context: CommandContext,
    options: MergeOptions,
) -> int:
    target = require_github_target(resolve_github_target(context.jj_client.list_git_remotes()))
    async with context.open_github_client(repo=target.repo) as github_client:
        outcome, trunk, prepared_merge = await _request_when_checks_pass(
            context=context,
            options=options,
            target=target,
            github_client=github_client,
        )
        if isinstance(outcome, PendingMerge):
            _print_merge_result(outcome.result())
            _report_merge_requested(outcome, head_change_id=prepared_merge.stack.head.change_id)
            if options.no_wait:
                console.output(
                    t"After GitHub finishes merging, run "
                    t"{ui.cmd(f'jj-stack sync {prepared_merge.sync_head}')}.",
                )
                return 0
            result = await outcome.wait(github_client)
        else:
            result = outcome
        _print_merge_result(result)
        if result.blocked:
            return 1
        if not result.applied:
            return 0
        console.output("Updating the local stack after the completed merge:")
        try:
            exit_code = await retry_while_github_catches_up(
                lambda: _update_local_stack(
                    context=context,
                    github_client=github_client,
                    trunk=trunk,
                    prepared_merge=prepared_merge,
                    result=result,
                )
            )
        except OperationLockBusyError as error:
            _warn_incomplete_post_merge_sync(prepared_merge.sync_head, has_recovery_hint=True)
            error.hint = (
                t"After it finishes, run {ui.cmd(f'jj-stack sync {prepared_merge.sync_head}')}. "
                t"If that command already synced this stack, nothing is left to do."
            )
            raise
        except BaseException as error:
            _warn_incomplete_post_merge_sync(
                prepared_merge.sync_head, has_recovery_hint=error_hint(error) is not None
            )
            if isinstance(error, GithubClientError):
                raise CliError(
                    "Could not update the local stack after the completed merge.",
                    hint=t"Resolve the GitHub error, then run "
                    t"{ui.cmd(f'jj-stack sync {prepared_merge.sync_head}')}.",
                ) from error
            raise
        if exit_code:
            _warn_incomplete_post_merge_sync(prepared_merge.sync_head, has_recovery_hint=True)
        return exit_code


async def _request_when_checks_pass(
    *,
    context: CommandContext,
    options: MergeOptions,
    target: GithubTarget,
    github_client: GithubClient,
) -> tuple[MergeResult | PendingMerge, ObservedTrunk, PreparedMerge]:
    expected_changes: tuple[MergeChange, ...] | None = None
    previous_progress: tuple[str, ...] | None = None
    selected_revset = options.revset
    target_change_id: ChangeId | None = None
    while True:
        with operation_lock(context.state_store, command="merge", mutating=not options.dry_run):
            if expected_changes is None:
                selected_revset, target_change_id = _resolve_merge_target(
                    context=context, pr=options.pr, revset=options.revset
                )
            with console.spinner(description="Inspecting jj stack"):
                prepared = prepare_local_stack(
                    containing_change_id=target_change_id if expected_changes is None else None,
                    context=context,
                    fetch_remote_state=expected_changes is None,
                    revset=selected_revset,
                )
            if prepared.github_target != target:
                raise CliError(
                    "The GitHub remote changed while preparing the merge.",
                    hint="Inspect it with jj-stack doctor, then rerun jj-stack merge.",
                )
            prepared_merge = PreparedMerge(
                context=context,
                options=options,
                stack=prepared.stack,
                state=prepared.state,
                target=target,
                target_change_id=target_change_id,
            )
            outcome, trunk = await _request_merge_async(
                prepared_merge,
                github_client,
                wait_for_checks=not (options.dry_run or options.no_wait),
                expected_changes=expected_changes,
            )
        if not isinstance(outcome, ChecksPending):
            return outcome, trunk, prepared_merge
        if previous_progress is None:
            console.output("Waiting for required checks. Ctrl-C stops before requesting a merge.")
        if outcome.progress != previous_progress:
            console.output("\n".join(outcome.progress))
        previous_progress = outcome.progress
        expected_changes = outcome.changes
        # Resolve a moving revset only once. Every later pass observes this same stack again,
        # including local edits and saved links changed by commands run while the lock was free.
        selected_revset = prepared.stack.head.change_id
        await outcome.wait()


async def _update_local_stack(
    *,
    context: CommandContext,
    github_client: GithubClient,
    trunk: ObservedTrunk,
    prepared_merge: PreparedMerge,
    result: MergeResult,
) -> int:
    """Sync the merged stack, or only clean up when the fetch already removed its changes."""

    head = prepared_merge.stack.head
    run = GithubRun(
        context=context,
        dry_run=False,
        github=github_client,
        target=prepared_merge.target,
        trunk=trunk,
    )
    with operation_lock(context.state_store, command="merge"):
        with console.spinner(description="Fetching trunk"):
            context.jj_client.fetch_remote(remote=prepared_merge.target.remote.name)
        if context.jj_client.query_commits_by_change_ids((head.change_id,))[head.change_id]:
            return await converge_selected_stack(run, revset=head.change_id)
        return await cleanup_stack_without_local_copies(
            run, change_id=result.merged_change_ids[-1]
        )


def _warn_incomplete_post_merge_sync(sync_head: str, *, has_recovery_hint: bool) -> None:
    console.warning(
        (
            t"GitHub completed the merge, but the follow-up work did not finish. Do not run "
            t"{ui.cmd('jj-stack merge')} again.",
            (
                t" Continue with {ui.cmd(f'jj-stack sync {sync_head}')}."
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
) -> tuple[str | None, ChangeId | None]:
    if pr is not None:
        resolved_revset, note = resolve_linked_change_for_pr(
            context=context,
            pr_reference=pr,
            revset=revset,
        )
        console.note(note)
        return None, resolved_revset
    return revset, None


async def _request_merge_async(
    prepared_merge: PreparedMerge,
    github_client: GithubClient,
    *,
    wait_for_checks: bool,
    expected_changes: tuple[MergeChange, ...] | None,
) -> tuple[MergeResult | PendingMerge | ChecksPending, ObservedTrunk]:
    stack = prepared_merge.stack
    github_repo = prepared_merge.target.repo
    remote = prepared_merge.target.remote

    with console.spinner(description="Inspecting remotes"):
        github_repo_state = await observe_github_repo(github_client, hint=_RERUN_HINT)
        trunk_branch, _trunk_targets = observe_trunk_branch(
            jj_client=prepared_merge.context.jj_client,
            github_repo_state=github_repo_state,
            remote=remote,
            trunk_commit_id=stack.trunk.commit_id,
        )
    trunk = ObservedTrunk(github_repo=github_repo_state, branch=trunk_branch)
    queue_task = asyncio.create_task(
        read_or_stop(
            github_client.base_branch_uses_merge_queue(branch=trunk_branch),
            message=t"Could not check whether {ui.bookmark(trunk_branch)} uses a merge queue.",
            hint=_RERUN_HINT,
        )
    )
    prs_task = asyncio.create_task(
        read_or_stop(
            observe_prs(
                change_ids=tuple(change.change_id for change in stack.changes),
                context=prepared_merge.context,
                github_client=github_client,
                github_repo_snapshot=github_repo_state,
                remote_name=remote.name,
                state=prepared_merge.state,
            ),
            message="Could not inspect GitHub state for merge.",
            hint=_RERUN_HINT,
        )
    )
    stacks_task = asyncio.create_task(observe_github_stacks(github=github_client))
    await wait_for_read_tasks(queue_task, prs_task, stacks_task)
    if queue_task.result():
        method = None
    else:
        method = _resolve_merge_method(
            changes=stack.changes,
            configured=prepared_merge.context.config.merge_method,
            merge_method=prepared_merge.options.merge_method,
            repo_state=github_repo_state,
        )
    plan = build_merge_plan(
        observation=prs_task.result(),
        repo=github_repo,
        changes=stack.changes,
        target_change_id=prepared_merge.target_change_id,
    )
    if expected_changes is not None and plan.planned_changes != expected_changes:
        raise CliError(
            "The selected stack changed while waiting for required checks.",
            hint="Inspect it with jj-stack view, then rerun jj-stack merge.",
        )
    stacks = stacks_task.result()
    execution = MergeExecutionInputs(
        repo=github_client.repo,
        selected_head=short_change_id(prepared_merge.target_change_id or stack.head.change_id),
        sync_head=prepared_merge.sync_head,
        trunk_branch=trunk_branch,
    )
    async_merge = build_async_merge_plan(plan, stacks, execution)
    if queue_task.result() and prepared_merge.options.merge_method is not None:
        if expected_changes is None:
            console.warning(
                t"The base branch {ui.bookmark(trunk_branch)} uses a merge queue; ignoring "
                t"{ui.cmd('--method')}."
            )
    if prepared_merge.options.dry_run:
        action = (
            async_merge.action(
                method=method,
                repo=execution.repo,
                trunk_branch=trunk_branch,
            )
            if async_merge.planned
            else None
        )
        return MergeResult(actions=async_merge.actions(action)), trunk
    prs = tuple(
        pr
        for change in async_merge.planned
        if (pr := prs_task.result().prs[change.change_id].pr) is not None
    )
    async_merge, prs = await prepare_merge_request(github_client, async_merge, execution, prs)
    if wait_for_checks and async_merge.planned:
        waiting = await inspect_required_checks(github_client, async_merge.planned, prs)
        if waiting is not None:
            return waiting, trunk
    return await execute_async_merge(
        execution=execution,
        github=github_client,
        method=method,
        merge=async_merge,
    ), trunk


def _resolve_merge_method(
    *,
    changes: Sequence[LocalCommit],
    configured: MergeMethod | None,
    merge_method: MergeMethod | None,
    repo_state: GithubRepo,
) -> MergeMethod:
    """Honor explicit choices; require one for signed stacks with several allowed methods."""

    settings: dict[MergeMethod, bool | None] = {
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
            t"Stack contains signed commits ({ui.join(ui.change_id, signed)}), and this repo "
            t"allows several merge methods.",
            hint=t"Merging can discard signatures, so choose a method with {ui.cmd('--method')} "
            t"or {ui.code('jj-stack.merge_method')}.",
        )
    return allowed_methods[0]


def _report_merge_requested(pending: PendingMerge, *, head_change_id: ChangeId) -> None:
    # From here, stopping the command no longer stops the merge.
    pr_number = pending.merge.target.identity.pr_number
    console.jsonl_record(
        {
            "type": "merge_requested",
            "pr": {
                "number": pr_number,
                "url": pr_url(pr_number, repo=pending.execution.repo, url=None),
            },
            "state": pending.request.status,
            "head_change_id": head_change_id,
        }
    )


def _print_merge_result(result: MergeResult) -> None:
    console.output(_result_header(result))
    for action in result.actions:
        console.action_row(
            kind="stop" if action.kind == "boundary" else action.kind,
            status=action.status,
            body=action.body,
        )
    if result.final_trunk_commit_id is not None:
        console.output(
            t"GitHub reported final trunk commit {ui.commit_id(result.final_trunk_commit_id)}."
        )


def _result_header(result: MergeResult) -> str:
    if result.pending == "enqueued":
        return "In merge queue:"
    if result.pending:
        return "Merge requested:"
    if result.applied:
        return "Merge completed:"
    if result.blocked:
        return "Merge blocked:"
    return "Merge preview:"
