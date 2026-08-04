"""Ask GitHub to merge reviewed changes at the bottom of a stack.

Candidates are the consecutive open, non-draft pull requests from the bottom. Each must still
match the exact commit last submitted; GitHub decides whether reviews, checks, conflicts, and
repository rules allow the merge.

For a direct merge, the command waits for GitHub to finish. It then fetches trunk, removes the
merged changes from the local stack, rebases any remaining changes onto the updated trunk, and
updates their existing pull requests.

When the trunk branch uses a merge queue, the command adds the changes to the queue and exits as
soon as GitHub accepts them. It does not wait for them to merge or update the local stack. After
GitHub finishes, run `jj-stack sync <head-change-id>`.

Common examples:

- `jj-stack merge --dry-run` previews the merge without changing GitHub.

- `jj-stack merge` asks GitHub to merge the ready bottom changes.

- `jj-stack merge --pull-request 123 --method squash` stops at one linked PR and chooses the
  merge method explicitly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._github_stack_safety import GithubStackSelection
from jj_stack.commands.sync import run_stack_convergence
from jj_stack.config import MergeMethod
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import resolve_trunk_branch
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubRepository
from jj_stack.review.observation import observe_reviews
from jj_stack.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_stack.review.status import prepare_status
from jj_stack.state.operation_lock import acquire_operation_lock

from .github_stack import (
    build_async_merge_plan,
    execute_async_merge,
    validate_terminal_retry,
)
from .models import MergeExecutionInputs, MergeResult, PreparedMerge
from .plan import build_merge_plan
from .render import print_merge_result

HELP = "Merge the reviewed changes at the bottom of a stack"


def merge(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    merge_method: str | None,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="merge",
    ):
        return _run_merge(
            context=context,
            dry_run=dry_run,
            merge_method=merge_method,
            pull_request=pull_request,
            revset=revset,
        )


def _run_merge(
    *,
    context: CommandContext,
    dry_run: bool,
    merge_method: str | None,
    pull_request: str | None,
    revset: str | None,
) -> int:
    selected_revset, target_change_id = _resolve_merge_target(
        context=context,
        pull_request=pull_request,
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
    sync_change_id = prepared_merge.prepared_status.prepared.stack.head.change_id
    console.output("Updating the local stack after the completed merge:")
    try:
        exit_code = run_stack_convergence(
            context=context,
            dry_run=False,
            fetch_remote_state=True,
            revset=sync_change_id,
        )
    except BaseException as error:
        _warn_incomplete_post_merge_sync(sync_change_id)
        if isinstance(error, GithubClientError):
            raise CliError(
                "Could not update the local stack after the completed merge.",
                hint=t"Resolve the GitHub error, then run "
                t"{ui.cmd(f'jj-stack sync {sync_change_id}')}",
            ) from error
        raise
    if exit_code:
        _warn_incomplete_post_merge_sync(sync_change_id)
    return exit_code


def _warn_incomplete_post_merge_sync(sync_change_id: str) -> None:
    console.warning(
        t"GitHub completed the merge, but the local stack update did not finish. Do not run "
        t"{ui.cmd('jj-stack merge')} again. Continue with "
        t"{ui.cmd(f'jj-stack sync {sync_change_id}')}."
    )


def _resolve_merge_target(
    *,
    context: CommandContext,
    pull_request: str | None,
    revset: str | None,
) -> tuple[str | None, str | None]:
    if pull_request is not None:
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}")
        return None, resolved_revset
    return (
        resolve_selected_revset(
            command_label="merge",
            default_revset=None,
            require_explicit=False,
            revset=revset,
        ),
        None,
    )


def _prepare_merge(
    *,
    context: CommandContext,
    dry_run: bool,
    merge_method: str | None,
    revset: str | None,
    target_change_id: str | None,
) -> PreparedMerge:
    prepared_status = prepare_status(
        containing_change_id=target_change_id,
        context=context,
        fetch_remote_state=True,
        re_resolve_after_remote_refresh=True,
        revset=revset,
    )
    prepared = prepared_status.prepared
    if prepared.remote is None:
        message = prepared.remote_error or t"Could not determine which Git remote to use."
        raise CliError(
            message,
            hint=t"Configure one GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
    if prepared_status.github_repository is None:
        message = prepared_status.github_repository_error or t"Could not resolve GitHub target."
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
        prepared_status=prepared_status,
        target_change_id=target_change_id,
    )


async def _stream_merge_async(
    *,
    prepared_merge: PreparedMerge,
) -> MergeResult:
    prepared_status = prepared_merge.prepared_status
    prepared = prepared_status.prepared
    github_repository = prepared_status.github_repository
    remote = prepared.remote
    if github_repository is None or remote is None:
        raise AssertionError("Prepared merge requires resolved GitHub and remote targets.")

    async with build_github_client(repository=github_repository) as github_client:
        try:
            github_repository_state = await github_client.get_repository()
        except GithubClientError as error:
            raise CliError(
                t"Could not load GitHub repository {github_repository.full_name}",
                hint="Resolve the GitHub error above, then rerun merge.",
            ) from error
        with console.spinner(description="Loading remote branches"):
            trunk_branch, _trunk_targets = resolve_trunk_branch(
                client=prepared.client,
                github_repository_state=github_repository_state,
                remote=remote,
                trunk_commit_id=prepared.stack.trunk.commit_id,
            )
        try:
            uses_merge_queue = await github_client.base_branch_uses_merge_queue(
                branch=trunk_branch
            )
        except GithubClientError:
            uses_merge_queue = False
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
                configured=prepared_merge.context.config.merge_method,
                merge_method=prepared_merge.merge_method,
                repository_state=github_repository_state,
            )
        try:
            observation = await observe_reviews(
                change_ids=tuple(revision.change_id for revision in prepared.stack.revisions),
                context=prepared_merge.context,
                github_client=github_client,
                remote_name=remote.name,
            )
        except GithubClientError as error:
            raise CliError(
                "Could not inspect GitHub state for merge.",
                hint="Resolve the GitHub error above, then rerun merge.",
            ) from error

        plan = build_merge_plan(
            observation=observation,
            remote_name=remote.name,
            repository=github_repository,
            revisions=prepared.stack.revisions,
            state=prepared.state,
            target_change_id=prepared_merge.target_change_id,
            trunk_branch=trunk_branch,
        )
        selection = GithubStackSelection(
            github_client,
            tuple(revision.identity.pr_number for revision in plan.reviewed_revisions),
        )
        stacks = await selection.observe()
        async_merge = build_async_merge_plan(
            plan,
            stacks,
            prepared_merge.target_change_id,
        )
        execution = MergeExecutionInputs(
            remote_name=remote.name,
            selected_revset=prepared_status.selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=prepared.stack.trunk.subject,
        )
        if async_merge.planned and async_merge.terminal_retry:
            validate_terminal_retry(
                execution=execution,
                github=github_client,
                merge=async_merge,
                observation=observation,
            )
        if prepared_merge.dry_run:
            if async_merge.planned:
                action = async_merge.action(
                    merge_action=merge_action,
                    method=resolved_merge_method,
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
    configured: MergeMethod | None,
    merge_method: str | None,
    repository_state: GithubRepository,
) -> str:
    """Choose the merge method, preferring this run's flag over the repository's configuration.

    GitHub reports which methods a repository allows but never which one to prefer, so a
    repository that allows several needs the choice made here.
    """

    settings = {
        "merge": repository_state.allow_merge_commit,
        "rebase": repository_state.allow_rebase_merge,
        "squash": repository_state.allow_squash_merge,
    }
    if any(allowed is None for allowed in settings.values()):
        if merge_method is not None:
            return merge_method
        raise CliError(
            "GitHub did not report which merge methods this repository allows.",
            hint=t"Pass {ui.cmd('--method')} explicitly.",
        )
    allowed_methods = sorted(method for method, allowed in settings.items() if allowed)
    if not allowed_methods:
        raise CliError(
            "This repository does not allow any pull request merge method.",
            hint="Fix the repository merge settings on GitHub before merging.",
        )
    chosen = merge_method or configured
    if chosen is not None:
        if chosen not in allowed_methods:
            source = ui.cmd("--method") if merge_method else ui.code("jj-stack.merge_method")
            raise CliError(
                t"This repository does not allow {ui.cmd(chosen)} merges; it allows "
                t"{ui.join(ui.cmd, allowed_methods)}.",
                hint=t"Change {source} to one it allows, or enable {ui.cmd(chosen)} on GitHub.",
            )
        return chosen
    if len(allowed_methods) == 1:
        return allowed_methods[0]
    raise CliError(
        t"This repository allows more than one merge method "
        t"({ui.join(ui.cmd, allowed_methods)}).",
        hint=t"Pass {ui.cmd('--method')}, or set it once with "
        t"{ui.cmd('jj config set --repo jj-stack.merge_method squash')}.",
    )
