"""Land the consecutive changes above `trunk()` that are ready to land now.

If your stack isn't based off `trunk()`, you'll need to `rebase` before landing.

To determine what to land, `land` walks up the stack until it reaches the top or a change that
it cannot land.

For a change to be landed, it must have no unresolved merge/rebase conflicts. Also, each pull
request must be open, not draft, approved, and have no outstanding changes requested. Use
`--bypass-readiness` to skip the draft / approval / changes-requested readiness checks.

Use `--dry-run` to inspect the landing plan without changing jj or GitHub state.

Use `--pull-request` to select the top of the stack to land by PR number or URL.

After a successful land, `jj-review` forgets the bookmarks it was managing for the changes that
landed, unless they've been moved or become conflicted. If you used your own bookmarks with
`submit --use-bookmarks`, they will not be cleaned up by default (override with `--config
jj-review.cleanup_user_bookmarks=true`). Use `--skip-cleanup` to keep even `jj-review`'s own
review bookmarks.

`land` does not touch changes above the first that could not be landed. In the usual direct-push
path, those remaining local changes keep the same base they already had, so no local rebase is
needed just because lower changes landed. Run `cleanup --rebase` only when some lower changes
were merged through different commit IDs and the local stack still contains those merged
ancestors; after that local rewrite, run `submit` to refresh the surviving review branches and
pull requests on GitHub.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import jj_review.console as console
import jj_review.ui as ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.errors import CliError
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClientError, build_github_client
from jj_review.github.resolution import (
    resolve_trunk_branch,
)
from jj_review.jj.client import JjCliArgs
from jj_review.models.review_state import ReviewState
from jj_review.review.change_status import classify_review_status_revision
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_review.review.status import (
    PreparedStatus,
    StatusResult,
    prepare_status,
    stream_status,
)
from jj_review.state.journal import JournalEvent, read_operation_log
from jj_review.state.operation_lock import acquire_operation_lock

from .execute import (
    ensure_trunk_branch_matches_selected_trunk,
    execute_land_plan,
)
from .models import (
    LandPlan,
    LandResult,
    LandRevision,
    PreparedLand,
)
from .plan import (
    build_land_plan,
    plan_review_bookmark_cleanup_for_revisions,
)
from .render import print_land_result

HELP = "Land the ready changes at the bottom of a stack"


def land(
    *,
    bypass_readiness: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
    skip_cleanup: bool,
) -> int:
    """CLI entrypoint for `land`."""

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
            pull_request=pull_request,
            revset=revset,
        )


def _run_land(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    context: CommandContext,
    dry_run: bool,
    pull_request: str | None,
    revset: str | None,
) -> int:
    selected_pr_number, selected_revset = _resolve_land_target(
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
            revset=selected_revset,
            selected_pr_number=selected_pr_number,
        )
    result = _stream_land(prepared_land=prepared_land)
    print_land_result(result)
    return 1 if result.blocked else 0


def _resolve_land_target(
    *,
    context: CommandContext,
    pull_request: str | None,
    revset: str | None,
) -> tuple[int | None, str | None]:
    if pull_request is not None:
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="land",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(
            t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}"
        )
        return pull_request_number, resolved_revset
    return (
        None,
        resolve_selected_revset(
            command_label="land",
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        ),
    )


def _prepare_land(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    context: CommandContext,
    dry_run: bool,
    revset: str | None,
    selected_pr_number: int | None,
) -> PreparedLand:
    """Resolve local landing inputs before GitHub planning and execution."""

    prepared_status = prepare_status(
        context=context,
        fetch_remote_state=True,
        re_resolve_after_remote_refresh=True,
        revset=revset,
    )
    prepared = prepared_status.prepared
    if prepared.remote is None:
        message = prepared.remote_error or t"Could not determine which Git remote to use."
        raise CliError(message)
    if prepared_status.github_repository is None:
        message = prepared_status.github_repository_error or t"Could not resolve GitHub target."
        raise CliError(message)

    if not dry_run:
        prepared.state_store.require_writable()
    return PreparedLand(
        cleanup_bookmarks=cleanup_bookmarks,
        dry_run=dry_run,
        bypass_readiness=bypass_readiness,
        context=context,
        prepared_status=prepared_status,
        selected_pr_number=selected_pr_number,
    )


def _stream_land(*, prepared_land: PreparedLand) -> LandResult:
    """Inspect GitHub state for the prepared path and optionally execute `land`."""

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
    selected_stack_is_off_trunk = (
        bool(prepared.stack.revisions)
        and prepared.stack.base_parent.commit_id != prepared.stack.trunk.commit_id
    )
    if selected_stack_is_off_trunk:
        raise _stack_not_on_trunk_error(
            prepared_status=prepared_status,
            status_result=status_result,
        )

    github_repository = prepared_status.github_repository
    remote = prepared.remote
    if github_repository is None or remote is None:
        raise AssertionError("Prepared land requires resolved GitHub and remote targets.")

    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            github_repository_state = await github_client.get_repository(
                github_repository.owner,
                github_repository.repo,
            )
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
        ensure_trunk_branch_matches_selected_trunk(
            client=prepared.client,
            remote_name=remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )

        async def finish_plan(plan: LandPlan) -> LandResult:
            bookmark_cleanup_plans = plan_review_bookmark_cleanup_for_revisions(
                bookmark_states=bookmark_states,
                prefix=prepared_land.context.config.bookmark_prefix,
                cleanup_bookmarks=prepared_land.cleanup_bookmarks,
                cleanup_user_bookmarks=prepared_land.context.config.cleanup_user_bookmarks,
                planned_revisions=plan.planned_revisions,
            )
            if prepared_land.dry_run:
                return LandResult(
                    actions=plan.planned_actions(
                        bookmark_cleanup_plans=bookmark_cleanup_plans,
                    ),
                    applied=False,
                    bypass_readiness=prepared_land.bypass_readiness,
                    blocked=plan.blocked,
                    github_repository=github_repository.full_name,
                    remote_name=remote.name,
                    selected_revset=status_result.selected_revset,
                    trunk_branch=trunk_branch,
                    trunk_subject=prepared.stack.trunk.subject,
                )
            return await execute_land_plan(
                bookmark_cleanup_plans=bookmark_cleanup_plans,
                github_client=github_client,
                github_repository=github_repository,
                plan=plan,
                prepared_land=prepared_land,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=prepared.stack.trunk.subject,
            )

        completion_plan = _land_completion_plan_from_log(
            prepared_land=prepared_land,
            trunk_branch=trunk_branch,
        )
        if completion_plan is not None:
            return await finish_plan(completion_plan)

        plan = build_land_plan(
            bypass_readiness=prepared_land.bypass_readiness,
            client=prepared.client,
            prepared_status=prepared_status,
            status_result=status_result,
            trunk_branch=trunk_branch,
        )
        return await finish_plan(plan)


def _land_completion_plan_from_log(
    *,
    prepared_land: PreparedLand,
    trunk_branch: str,
) -> LandPlan | None:
    """Build a post-trunk land completion plan from log evidence and current state."""

    prepared = prepared_land.prepared_status.prepared
    state = prepared.state_store.load()
    begin_event = _latest_interrupted_land_with_trunk_push(
        events=read_operation_log(prepared.state_store.state_dir),
        selected_head_change_id=prepared.stack.head.change_id,
    )
    if begin_event is None:
        return None
    planned_revisions = _logged_land_revisions(begin_event)
    if not planned_revisions:
        return None

    commit_ids = tuple(revision.commit_id for revision in planned_revisions)
    trunk_ancestor_commit_ids = prepared.client.query_trunk_ancestor_commit_ids(commit_ids)
    if set(commit_ids) - trunk_ancestor_commit_ids:
        raise CliError(
            "Cannot finish the interrupted land because the logged landed commits "
            f"are not all on {ui.revset('trunk()')}.",
            hint="Inspect the operation log and current trunk before retrying.",
        )

    for revision in planned_revisions:
        _ensure_logged_land_matches_saved_state(revision=revision, state=state)

    return LandPlan(
        blocked=False,
        boundary_action=None,
        planned_revisions=planned_revisions,
        push_trunk=False,
        trunk_branch=trunk_branch,
    )


def _latest_interrupted_land_with_trunk_push(
    *,
    events: tuple[JournalEvent, ...],
    selected_head_change_id: str,
) -> JournalEvent | None:
    completed_operation_ids = {
        event.operation_id
        for event in events
        if event.operation == "land" and event.event == "completed"
    }
    pushed_trunk_operation_ids = {
        event.operation_id
        for event in events
        if event.operation == "land"
        and event.event == "mutation_applied"
        and event.data.get("mutation") == "push_trunk"
    }
    for event in reversed(events):
        if event.operation != "land" or event.event != "begin":
            continue
        if event.operation_id in completed_operation_ids:
            continue
        if event.operation_id not in pushed_trunk_operation_ids:
            continue
        scope = event.data.get("resolved_scope", {})
        ordered_change_ids = tuple(scope.get("ordered_change_ids", ()))
        if selected_head_change_id in ordered_change_ids:
            return event
    return None


def _logged_land_revisions(event: JournalEvent) -> tuple[LandRevision, ...]:
    scope = event.data.get("resolved_scope", {})
    planned_revisions = scope.get("planned_revisions")
    if not isinstance(planned_revisions, list):
        return ()
    return tuple(
        LandRevision(
            bookmark=raw_revision["bookmark"],
            bookmark_managed=raw_revision.get("bookmark_managed", True),
            change_id=raw_revision["change_id"],
            commit_id=raw_revision["commit_id"],
            needs_resubmit=False,
            pull_request_number=raw_revision["pull_request_number"],
            subject=raw_revision["subject"],
        )
        for raw_revision in planned_revisions
    )


def _ensure_logged_land_matches_saved_state(
    *,
    revision: LandRevision,
    state: ReviewState,
) -> None:
    cached_change = state.changes.get(revision.change_id)
    if (
        cached_change is None
        or cached_change.link_state != "active"
        or cached_change.bookmark != revision.bookmark
        or cached_change.pr_number != revision.pull_request_number
    ):
        raise CliError(
            t"Cannot finish the interrupted land because saved review identity for "
            t"{ui.change_id(revision.change_id)} no longer matches the logged land.",
            hint=t"Run {ui.cmd('status --fetch')} and inspect the stack before retrying.",
        )


def _stack_not_on_trunk_error(
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
) -> CliError:
    message = t"Selected stack is not based on the current {ui.revset('trunk()')}."
    if any(
        classify_review_status_revision(revision).pr_lifecycle == "merged"
        for revision in status_result.revisions
    ):
        return CliError(
            message,
            hint=(
                t"Some lower changes from this stack already landed. Run "
                t"{ui.cmd('cleanup --rebase')} {ui.revset(status_result.selected_revset)} "
                t"to rebase the remaining local changes before retrying."
            ),
        )

    bottom_change_id = prepared_status.prepared.status_revisions[0].revision.change_id
    rebase_command = f"jj rebase -s {short_change_id(bottom_change_id)} -d 'trunk()'"
    return CliError(
        message,
        hint=(
            t"No change in the selected stack has landed yet. Move the whole stack onto "
            t"{ui.revset('trunk()')} with {ui.cmd(rebase_command)} before retrying."
        ),
    )
