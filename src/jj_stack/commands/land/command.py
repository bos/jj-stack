"""Land the consecutive changes above `trunk()` that are ready to land now.

If your stack is not based on `trunk()`, inspect it first. Run
`sync <head-change-id>` when a lower PR landed through another route; use plain `jj rebase` when
trunk advanced without one of your changes landing.

To determine what to land, `land` walks up the stack until it reaches the top or a change that
it cannot land.

For a change to be landed, it must have no unresolved merge/rebase conflicts. Also, each pull
request must be open, not draft, approved, and have no outstanding changes requested. Use
`--bypass-readiness` to skip the draft / approval / changes-requested readiness checks.

The local commit, the commit last sent for review, the review branch, and the PR head must match
exactly. After any rewrite, including a same-diff rebase, rerun `submit` before `land`.
Immediately before changing trunk or a pull request, `land` rechecks the repository, trunk, PR,
exact commit, and readiness. It stops if any of those changed since planning.

Use `--dry-run` to inspect the landing plan. It fetches remote state, which can update jj's
remembered remote bookmark locations, but does not change trunk, review branches, PRs, local
bookmarks, or tracking data.

Use `--pull-request` to select the top of the stack to land by PR number or URL.

By default `land` pushes the trunk branch directly. When branch protection requires pull requests,
use `--via merge` unless the repository requires a merge queue, which `jj-stack` cannot drive.
Each ready PR is retargeted to trunk and merged through GitHub, bottom to top, stopping at the
first PR GitHub reports as not mergeable. The merge method comes from `--merge-method`, or from
the repository's settings when exactly one method is allowed. After each accepted merge, `land`
drops the landed changes from the selected local stack and updates only surviving PRs that already
exist. Unreviewed trailing work stays local.

If `land --via merge` is interrupted after GitHub accepted a merge, run
`sync --dry-run <head-change-id>` and then `sync <head-change-id>`. Rerun
`land --via merge <head-change-id>` if you still want to land the remaining PRs. If GitHub
accepted no merge, retry the original `land --via merge` command directly. If a direct trunk push
succeeded but PR cleanup did not, run `sync --all --dry-run` and then `sync --all`.

After a successful land, `jj-stack` removes tracking for each landed change unless another local
stack still depends on it. It forgets managed bookmarks when they are safe to remove; bookmarks
that moved, became conflicted, or are still needed by another stack remain. If you used your own
bookmarks with `submit --use-bookmarks`, they will not be cleaned up by default (override with
`--config jj-stack.cleanup_user_bookmarks=true`). Use `--skip-cleanup` to keep even `jj-stack`'s
own review bookmarks.

`land` does not touch changes above the first that could not be landed. In the direct-push
path, those remaining local changes keep the same base they already had, so no local rebase is
needed just because lower changes landed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.commands.sync import run_stack_convergence
from jj_stack.errors import CliError, DriftError, UsageError
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

from .execute import execute_land_plan
from .models import (
    LandExecutionInputs,
    LandPlan,
    LandResult,
    LandVia,
    PreparedLand,
)
from .plan import (
    build_land_plan,
    plan_review_bookmark_cleanup_for_revisions,
    validate_land_plan_merge_method,
)
from .render import print_land_result

HELP = "Land the ready changes at the bottom of a stack"


def land(
    *,
    bypass_readiness: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    merge_method: str | None,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
    skip_cleanup: bool,
    via: LandVia,
) -> int:
    if merge_method is not None and via != "merge":
        raise UsageError(t"{ui.cmd('--merge-method')} is only used with {ui.cmd('--via merge')}.")
    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="land",
    ):
        return _run_land(
            bypass_readiness=bypass_readiness,
            cleanup_bookmarks=not skip_cleanup,
            context=context,
            dry_run=dry_run,
            merge_method=merge_method,
            pull_request=pull_request,
            revset=revset,
            via=via,
        )


def _run_land(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    context: CommandContext,
    dry_run: bool,
    merge_method: str | None,
    pull_request: str | None,
    revset: str | None,
    via: LandVia,
) -> int:
    selected_revset = _resolve_land_target(
        context=context,
        pull_request=pull_request,
        revset=revset,
    )
    with console.spinner(description="Inspecting jj stack"):
        prepared_land = _prepare_land(
            bypass_readiness=bypass_readiness,
            cleanup_bookmarks=cleanup_bookmarks,
            context=context,
            dry_run=dry_run,
            merge_method=merge_method,
            revset=selected_revset,
            via=via,
        )
    result = _stream_land(prepared_land=prepared_land)
    print_land_result(result)
    if result.merged_change_ids and not dry_run:
        convergence_exit = run_stack_convergence(
            context=context,
            dry_run=False,
            fetch_remote_state=False,
            revset=result.selected_revset,
        )
        if convergence_exit != 0:
            return 1
    return 1 if result.blocked else 0


def _resolve_land_target(
    *,
    context: CommandContext,
    pull_request: str | None,
    revset: str | None,
) -> str | None:
    if pull_request is not None:
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="land",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}")
        return resolved_revset
    return resolve_selected_revset(
        command_label="land",
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )


def _prepare_land(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    context: CommandContext,
    dry_run: bool,
    merge_method: str | None,
    revset: str | None,
    via: LandVia,
) -> PreparedLand:
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
                hint=t"Repair it with {ui.cmd('relink')} before landing the review.",
            )
    if prepared.remote is None:
        message = prepared.remote_error or t"Could not determine which Git remote to use."
        raise CliError(message)
    if prepared_status.github_repository is None:
        message = prepared_status.github_repository_error or t"Could not resolve GitHub target."
        raise CliError(message)

    if not dry_run:
        context.state_store.require_writable()
    return PreparedLand(
        cleanup_bookmarks=cleanup_bookmarks,
        dry_run=dry_run,
        bypass_readiness=bypass_readiness,
        context=context,
        merge_method=merge_method,
        prepared_status=prepared_status,
        via=via,
    )


def _stream_land(*, prepared_land: PreparedLand) -> LandResult:
    prepared_status = prepared_land.prepared_status
    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            inspect_stack_comments=False,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    return asyncio.run(
        _stream_land_async(
            prepared_land=prepared_land,
            status_result=status_result,
        )
    )


async def _stream_land_async(
    *,
    prepared_land: PreparedLand,
    status_result: StatusResult,
) -> LandResult:
    prepared_status = prepared_land.prepared_status
    prepared = prepared_status.prepared
    if status_result.github_error is not None:
        raise CliError(
            t"Could not inspect GitHub pull request state for {ui.cmd('land')}: "
            t"{status_result.github_error}"
        )
    github_repository = prepared_status.github_repository
    remote = prepared.remote
    if github_repository is None or remote is None:
        raise AssertionError("Prepared land requires resolved GitHub and remote targets.")

    async with build_github_client(repository=github_repository) as github_client:
        try:
            github_repository_state = await github_client.get_repository()
        except GithubClientError as error:
            raise CliError(
                t"Could not load GitHub repository {github_repository.full_name}"
            ) from error
        with console.spinner(description="Loading bookmark state"):
            bookmark_states = prepared.client.list_bookmark_states()
            trunk_branch = resolve_trunk_branch(
                bookmark_states=bookmark_states,
                github_repository_state=github_repository_state,
                remote_name=remote.name,
                trunk_commit_id=prepared.stack.trunk.commit_id,
            )
        resolved_merge_method: str | None = None
        if prepared_land.via == "merge":
            resolved_merge_method = _resolve_land_merge_method(
                merge_method=prepared_land.merge_method,
                repository_state=github_repository_state,
            )

        async def finish_plan(plan: LandPlan) -> LandResult:
            validate_land_plan_merge_method(merge_method=resolved_merge_method, plan=plan)
            execution = LandExecutionInputs(
                bypass_readiness=prepared_land.bypass_readiness,
                cleanup_bookmarks=prepared_land.cleanup_bookmarks,
                context=prepared_land.context,
                native_stacks=GithubStackSelection(
                    github_client,
                    tuple(revision.identity.pr_number for revision in plan.planned_revisions),
                    prepared_land.context.state_store,
                ),
            )
            if prepared_land.dry_run:
                bookmark_cleanup_actions = plan_review_bookmark_cleanup_for_revisions(
                    bookmark_states=bookmark_states,
                    prefix=prepared_land.context.config.bookmark_prefix,
                    cleanup_bookmarks=prepared_land.cleanup_bookmarks,
                    cleanup_user_bookmarks=(prepared_land.context.config.cleanup_user_bookmarks),
                    planned_revisions=plan.planned_revisions,
                )
                return LandResult(
                    actions=plan.planned_actions(
                        bookmark_cleanup_actions=bookmark_cleanup_actions,
                    ),
                    applied=False,
                    blocked=plan.blocked,
                    remote_name=remote.name,
                    selected_revset=status_result.selected_revset,
                    trunk_branch=trunk_branch,
                    trunk_subject=prepared.stack.trunk.subject,
                    via=plan.via,
                )
            return await execute_land_plan(
                execution=execution,
                github_client=github_client,
                merge_method=resolved_merge_method,
                plan=plan,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_commit_id=prepared.stack.trunk.commit_id,
                trunk_subject=prepared.stack.trunk.subject,
            )

        if prepared.stack.revisions and (
            prepared.stack.base_parent.commit_id != prepared.stack.trunk.commit_id
        ):
            raise _stack_not_on_trunk_error(
                prepared_status=prepared_status,
                status_result=status_result,
            )

        plan = build_land_plan(
            bypass_readiness=prepared_land.bypass_readiness,
            prepared_status=prepared_status,
            status_result=status_result,
            trunk_branch=trunk_branch,
            via=prepared_land.via,
        )
        return await finish_plan(plan)


def _resolve_land_merge_method(
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
            hint="Fix the repository merge settings on GitHub before landing.",
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
                t"before retrying land."
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
