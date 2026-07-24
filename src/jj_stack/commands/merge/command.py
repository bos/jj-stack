"""Ask GitHub to merge the reviewed changes at the bottom of a stack.

`merge` selects the contiguous bottom prefix of open, non-draft pull requests. GitHub decides
whether reviews, checks, conflicts, and repository rules allow each merge. The command processes
ordinary pull requests bottom-up and stops after the first rejection.

Every pull request must still match its saved tracking, and its live head, review branch,
submitted commit, and local commit must identify the same exact snapshot. Use `submit` after any
rewrite.

Use `--dry-run` to fetch and validate current state without changing GitHub. Use `--pull-request`
to select a target by pull request number or URL. The merge method comes from `--merge-method`, or
from the repository settings when exactly one method is enabled.

GitHub moves trunk. `merge` does not rewrite local history, refresh surviving review branches, or
remove tracking. After GitHub accepts a merge, run the selected `sync` command printed in the
result.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.errors import CliError, DriftError
from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import resolve_trunk_branch
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.github import GithubRepository
from jj_stack.review.change_status import classify_review_status_revision
from jj_stack.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_stack.review.status import (
    PreparedStatus,
    StatusResult,
    prepare_status,
    stream_status,
)
from jj_stack.state.operation_lock import acquire_operation_lock

from .execute import execute_merge_plan
from .models import MergeExecutionInputs, MergePlan, MergeResult, PreparedMerge
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
    selected_revset = _resolve_merge_target(
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
        )
    result = _stream_merge(prepared_merge=prepared_merge)
    print_merge_result(result)
    return 1 if result.blocked else 0


def _resolve_merge_target(
    *,
    context: CommandContext,
    pull_request: str | None,
    revset: str | None,
) -> str | None:
    if pull_request is not None:
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="merge",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}")
        return resolved_revset
    return resolve_selected_revset(
        command_label="merge",
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )


def _prepare_merge(
    *,
    context: CommandContext,
    dry_run: bool,
    merge_method: str | None,
    revset: str | None,
) -> PreparedMerge:
    prepared_status = prepare_status(
        context=context,
        fetch_remote_state=True,
        re_resolve_after_remote_refresh=True,
        revset=revset,
        validate_review_ownership=True,
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
        raise CliError(message)
    if prepared_status.github_repository is None:
        message = prepared_status.github_repository_error or t"Could not resolve GitHub target."
        raise CliError(message)

    if not dry_run:
        context.state_store.require_writable()
    return PreparedMerge(
        context=context,
        dry_run=dry_run,
        merge_method=merge_method,
        prepared_status=prepared_status,
    )


def _stream_merge(*, prepared_merge: PreparedMerge) -> MergeResult:
    prepared_status = prepared_merge.prepared_status
    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            inspect_stack_comments=False,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    return asyncio.run(
        _stream_merge_async(
            prepared_merge=prepared_merge,
            status_result=status_result,
        )
    )


async def _stream_merge_async(
    *,
    prepared_merge: PreparedMerge,
    status_result: StatusResult,
) -> MergeResult:
    prepared_status = prepared_merge.prepared_status
    prepared = prepared_status.prepared
    if status_result.github_error is not None:
        raise CliError(
            t"Could not inspect GitHub pull request state for {ui.cmd('merge')}: "
            t"{status_result.github_error}"
        )
    github_repository = prepared_status.github_repository
    remote = prepared.remote
    if github_repository is None or remote is None:
        raise AssertionError("Prepared merge requires resolved GitHub and remote targets.")

    async with build_github_client(repository=github_repository) as github_client:
        try:
            github_repository_state = await github_client.get_repository()
        except GithubClientError as error:
            raise CliError(
                t"Could not load GitHub repository {github_repository.full_name}"
            ) from error
        with console.spinner(description="Loading bookmark state"):
            trunk_branch = resolve_trunk_branch(
                bookmark_states=prepared.client.list_bookmark_states(),
                github_repository_state=github_repository_state,
                remote_name=remote.name,
                trunk_commit_id=prepared.stack.trunk.commit_id,
            )
        resolved_merge_method = _resolve_merge_method(
            merge_method=prepared_merge.merge_method,
            repository_state=github_repository_state,
        )

        if prepared.stack.revisions and (
            prepared.stack.base_parent.commit_id != prepared.stack.trunk.commit_id
        ):
            raise _stack_not_on_trunk_error(
                prepared_status=prepared_status,
                status_result=status_result,
            )

        plan = build_merge_plan(
            prepared_status=prepared_status,
            status_result=status_result,
            trunk_branch=trunk_branch,
        )
        validate_merge_plan_method(merge_method=resolved_merge_method, plan=plan)
        selection = GithubStackSelection(
            github_client,
            tuple(revision.identity.pr_number for revision in plan.planned_revisions),
            prepared_merge.context.state_store,
        )
        if prepared_merge.dry_run:
            if not plan.blocked:
                await selection.require_unstacked(persist=False)
            return _dry_run_result(
                plan=plan,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=prepared.stack.trunk.subject,
            )
        return await execute_merge_plan(
            execution=MergeExecutionInputs(
                context=prepared_merge.context,
                native_stacks=selection,
            ),
            github_client=github_client,
            merge_method=resolved_merge_method,
            plan=plan,
            remote_name=remote.name,
            selected_revset=status_result.selected_revset,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
            trunk_subject=prepared.stack.trunk.subject,
        )


def _dry_run_result(
    *,
    plan: MergePlan,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_subject: str,
) -> MergeResult:
    return MergeResult(
        actions=plan.planned_actions(),
        applied=False,
        blocked=plan.blocked,
        remote_name=remote_name,
        selected_revset=selected_revset,
        trunk_branch=trunk_branch,
        trunk_subject=trunk_subject,
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
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
) -> DriftError:
    message = t"Selected stack is not based on the current {ui.revset('trunk()')}."
    if any(
        classify_review_status_revision(revision).pr_lifecycle == "merged"
        for revision in status_result.revisions
    ):
        return DriftError(
            message,
            condition="merged_ancestor_on_trunk",
            hint=(
                t"Some lower changes from this stack already landed. Preview "
                t"{ui.cmd('jj-stack sync --dry-run')} "
                t"{ui.revset(status_result.selected_revset)}, then run "
                t"{ui.cmd('jj-stack sync')} {ui.revset(status_result.selected_revset)} "
                t"before retrying merge."
            ),
        )

    bottom_change_id = prepared_status.prepared.status_revisions[0].revision.change_id
    rebase_command = f"jj rebase -s {short_change_id(bottom_change_id)} -d 'trunk()'"
    return DriftError(
        message,
        condition="stack_not_on_trunk",
        hint=(
            t"No change in the selected stack has landed yet. Move the whole stack onto "
            t"{ui.revset('trunk()')} with {ui.cmd(rebase_command)} before retrying."
        ),
    )
