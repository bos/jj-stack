"""Ask GitHub to merge reviewed changes at the bottom of a stack.

Candidates are the consecutive open, non-draft pull requests from the bottom. Each must still
match the exact commit last submitted; GitHub decides whether reviews, checks, conflicts, and
repository rules allow the merge.

Repositories with GitHub stack support merge the selected changes together. Other repositories
merge pull requests bottom-up and stop at the first rejection. This command does not update local
history or remove review state; run the printed `jj-stack sync` command after GitHub merges
anything.

Common examples: `jj-stack merge --dry-run` previews the merge without changing GitHub;
`jj-stack merge` asks GitHub to merge the ready bottom changes; and
`jj-stack merge --pull-request 123 --merge-method squash` stops at one linked PR and chooses the
merge method explicitly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.errors import CliError, DriftError
from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import resolve_trunk_branch
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.github import GithubRepository
from jj_stack.review.discovery import discover_stacks_from_revisions
from jj_stack.review.observation import RepositoryObservation, observe_reviews
from jj_stack.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_stack.review.status import PreparedStatus, prepare_status
from jj_stack.state.operation_lock import acquire_operation_lock

from .execute import execute_merge_plan
from .models import MergeExecutionInputs, MergeResult, PreparedMerge
from .native import build_native_merge_plan, check_native_merge, execute_native_merge
from .plan import build_merge_plan, validate_merge_plan_method
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
    return 1 if result.blocked else 0


def _resolve_merge_target(
    *,
    context: CommandContext,
    pull_request: str | None,
    revset: str | None,
) -> tuple[str | None, str | None]:
    if pull_request is not None:
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="merge",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}")
        selected = context.jj_client.resolve_revision(resolved_revset)
        stacks = discover_stacks_from_revisions(
            jj_client=context.jj_client,
            revisions=(selected,),
            include_working_copies=True,
        )
        matching = tuple(
            stack
            for stack in stacks
            if resolved_revset in {revision.change_id for revision in stack.revisions}
        )
        if not matching:
            raise CliError(
                t"PR #{pull_request_number} is linked to {ui.change_id(resolved_revset)}, "
                t"which is not on any current review path.",
                hint=t"Run {ui.cmd('jj-stack view')} to find where that change went, or "
                t"{ui.cmd('jj-stack sync')} if it already merged.",
            )
        if len(matching) != 1:
            raise CliError(
                t"PR #{pull_request_number} belongs to more than one local review path.",
                hint=t"Run {ui.cmd('jj-stack merge <head-change-id>')} with the head of the "
                t"stack you want to merge.",
            )
        return matching[0].head.change_id, resolved_revset
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
        context=context,
        dry_run=dry_run,
        fetch_remote_state=True,
        on_fetch_isolation_change=report_fetch_isolation,
        re_resolve_after_remote_refresh=True,
        revset=revset,
    )
    prepared = prepared_status.prepared
    for revision in prepared.stack.revisions:
        if prepared.state.issues_for(revision.change_id):
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is malformed.",
                hint=t"Repair it with {ui.cmd('relink')} before merging the review.",
            )
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
        resolved_merge_method = _resolve_merge_method(
            merge_method=prepared_merge.merge_method,
            repository_state=github_repository_state,
        )
        try:
            observation = await observe_reviews(
                change_ids=tuple(revision.change_id for revision in prepared.stack.revisions),
                context=prepared_merge.context,
                github_client=github_client,
                remote_name=remote.name,
                trunk_branch=trunk_branch,
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
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        selection = GithubStackSelection(
            github_client,
            tuple(revision.identity.pr_number for revision in plan.reviewed_revisions),
            prepared_merge.context.state_store,
        )
        supported, stacks = await selection.observe(persist=not prepared_merge.dry_run)
        native = build_native_merge_plan(plan, stacks, supported, prepared_merge.target_change_id)
        if (
            prepared.stack.revisions
            and prepared.stack.base_parent.commit_id != prepared.stack.trunk.commit_id
            and (native is None or not native.terminal_retry)
        ):
            raise _stack_not_on_trunk_error(observation, prepared_status)
        if native is None:
            validate_merge_plan_method(merge_method=resolved_merge_method, plan=plan)
        execution = MergeExecutionInputs(
            context=prepared_merge.context,
            remote_name=remote.name,
            selected_revset=prepared_status.selected_revset,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
            trunk_subject=prepared.stack.trunk.subject,
        )
        if prepared_merge.dry_run:
            if native is not None:
                await check_native_merge(execution, github_client, native)
                return execution.result(actions=(native.action(resolved_merge_method),))
            return execution.result(actions=plan.planned_actions())
        if native is not None:
            return await execute_native_merge(
                execution=execution,
                github=github_client,
                merge_method=resolved_merge_method,
                native=native,
            )
        return await execute_merge_plan(
            execution=execution,
            github_client=github_client,
            merge_method=resolved_merge_method,
            plan=plan,
        )


def _resolve_merge_method(
    *,
    merge_method: str | None,
    repository_state: GithubRepository,
) -> str:
    if merge_method is not None:
        return merge_method
    settings = {
        "merge": repository_state.allow_merge_commit,
        "rebase": repository_state.allow_rebase_merge,
        "squash": repository_state.allow_squash_merge,
    }
    if any(allowed is None for allowed in settings.values()):
        raise CliError(
            "GitHub did not report which merge methods this repository allows.",
            hint=t"Pass {ui.cmd('--merge-method')} explicitly.",
        )
    allowed_methods = sorted(method for method, allowed in settings.items() if allowed)
    if len(allowed_methods) == 1:
        return allowed_methods[0]
    if not allowed_methods:
        raise CliError(
            "This repository does not allow any pull request merge method.",
            hint="Fix the repository merge settings on GitHub before merging.",
        )
    options = ui.join(ui.cmd, allowed_methods)
    raise CliError(
        t"This repository allows more than one merge method ({options}).",
        hint=t"Pass {ui.cmd('--merge-method')} to choose one.",
    )


def _stack_not_on_trunk_error(
    observation: RepositoryObservation,
    prepared_status: PreparedStatus,
) -> DriftError:
    message = t"Selected stack is not based on the current {ui.revset('trunk()')}."
    merged = any(
        review.pull_request is not None
        and review.pull_request.normalize_state().state == "merged"
        for review in observation.reviews.values()
    )
    if merged:
        condition = "merged_ancestor_on_trunk"
        hint = (
            t"Run {ui.cmd('jj-stack sync')} {ui.revset(prepared_status.selected_revset)} "
            t"before retrying merge."
        )
    else:
        condition = "stack_not_on_trunk"
        bottom = prepared_status.prepared.status_revisions[0].revision.change_id
        rebase = f"jj rebase -s {short_change_id(bottom)} -d 'trunk()'"
        hint = t"Run {ui.cmd(rebase)} before retrying merge."
    return DriftError(message, condition=condition, hint=hint)
