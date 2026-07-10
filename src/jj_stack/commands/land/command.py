"""Land the consecutive changes above `trunk()` that are ready to land now.

If your stack isn't based off `trunk()`, you'll need to `rebase` before landing.

To determine what to land, `land` walks up the stack until it reaches the top or a change that
it cannot land.

For a change to be landed, it must have no unresolved merge/rebase conflicts. Also, each pull
request must be open, not draft, approved, and have no outstanding changes requested. Use
`--bypass-readiness` to skip the draft / approval / changes-requested readiness checks.

Use `--dry-run` to inspect the landing plan without changing jj or GitHub state.

Use `--pull-request` to select the top of the stack to land by PR number or URL.

By default `land` pushes the trunk branch directly. When branch protection requires changes to
arrive through pull requests, use `--via merge` instead: each ready PR is retargeted to trunk
and merged through GitHub, bottom to top, stopping at the first PR GitHub reports as not
mergeable. The merge method comes from `--merge-method`, or from the repository's settings when
exactly one method is allowed. Merging on GitHub does not move your local history: afterwards,
run `sync` to rebase the remaining local stack off the merged changes.

After a successful land, `jj-stack` forgets the bookmarks it was managing for the changes that
landed, unless they've been moved or become conflicted. If you used your own bookmarks with
`submit --use-bookmarks`, they will not be cleaned up by default (override with `--config
jj-stack.cleanup_user_bookmarks=true`). Use `--skip-cleanup` to keep even `jj-stack`'s own
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

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError, UsageError
from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import (
    resolve_trunk_branch,
)
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.github import GithubRepository
from jj_stack.models.review_state import ReviewState
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
from jj_stack.state.journal import JournalEvent, read_operation_log
from jj_stack.state.operation_lock import acquire_operation_lock

from .execute import (
    ensure_trunk_branch_matches_selected_trunk,
    execute_land_plan,
)
from .models import (
    LandPlan,
    LandResult,
    LandRevision,
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
    """CLI entrypoint for `land`."""

    if merge_method is not None and via != "merge":
        raise UsageError(
            t"{ui.cmd('--merge-method')} is only used with {ui.cmd('--via merge')}."
        )
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
            merge_method=merge_method,
            revset=selected_revset,
            selected_pr_number=selected_pr_number,
            via=via,
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
    merge_method: str | None,
    revset: str | None,
    selected_pr_number: int | None,
    via: LandVia,
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
        merge_method=merge_method,
        prepared_status=prepared_status,
        selected_pr_number=selected_pr_number,
        via=via,
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

    async with build_github_client(repository=github_repository) as github_client:
        try:
            github_repository_state = await github_client.get_repository(
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
        resolved_merge_method: str | None = None
        if prepared_land.via == "merge":
            resolved_merge_method = _resolve_land_merge_method(
                merge_method=prepared_land.merge_method,
                repository_state=github_repository_state,
            )

        async def finish_plan(plan: LandPlan) -> LandResult:
            validate_land_plan_merge_method(
                merge_method=resolved_merge_method,
                plan=plan,
            )
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
                    remote_name=remote.name,
                    selected_revset=status_result.selected_revset,
                    trunk_branch=trunk_branch,
                    trunk_subject=prepared.stack.trunk.subject,
                    via=plan.via,
                )
            return await execute_land_plan(
                bookmark_cleanup_plans=bookmark_cleanup_plans,
                github_client=github_client,
                merge_method=resolved_merge_method,
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

        ensure_trunk_branch_matches_selected_trunk(
            client=prepared.client,
            remote_name=remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )

        plan = build_land_plan(
            bypass_readiness=prepared_land.bypass_readiness,
            client=prepared.client,
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
    """Resolve the GitHub merge method for `land --via merge`."""

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


def _land_completion_plan_from_log(
    *,
    prepared_land: PreparedLand,
    trunk_branch: str,
) -> LandPlan | None:
    """Build a post-trunk land completion plan from log evidence and current state."""

    prepared = prepared_land.prepared_status.prepared
    state = prepared.state_store.load()
    events = read_operation_log(prepared.state_store.state_dir)
    begin_event = _latest_incomplete_direct_push_land(
        events=events,
        selected_head_change_id=prepared.stack.head.change_id,
    )
    if begin_event is None:
        return None
    planned_revisions = _logged_land_revisions(begin_event)
    if not planned_revisions:
        return None

    commit_ids = tuple(revision.commit_id for revision in planned_revisions)
    remote = prepared.remote
    if remote is None:
        return None
    trunk_bookmark = prepared.client.get_bookmark_state(trunk_branch)
    remote_trunk = trunk_bookmark.remote_target(remote.name)
    remote_trunk_commit_id = None if remote_trunk is None else remote_trunk.target
    remote_trunk_ancestor_commit_ids = (
        set()
        if remote_trunk_commit_id is None
        else prepared.client.query_commit_ids_ancestors_of(
            commit_ids,
            descendant_commit_id=remote_trunk_commit_id,
        )
    )
    if set(commit_ids) - remote_trunk_ancestor_commit_ids:
        if not _land_log_records_applied_trunk_push(
            events=events,
            operation_id=begin_event.operation_id,
        ):
            # The durable begin record precedes the push. If the logged commits
            # did not reach trunk, the failed attempt did not move the remote
            # and an ordinary replan is safe.
            return None
        raise CliError(
            "Cannot finish the interrupted land because the logged landed commits "
            f"are not all on {ui.revset('trunk()')}.",
            hint="Inspect the operation log and current trunk before retrying.",
        )

    retired_change_ids = _logged_retired_change_ids(
        events=events,
        operation_id=begin_event.operation_id,
    )
    for revision in planned_revisions:
        _ensure_logged_land_matches_saved_state(
            retired_change_ids=retired_change_ids,
            revision=revision,
            state=state,
        )

    return LandPlan(
        blocked=False,
        boundary_action=None,
        planned_revisions=planned_revisions,
        push_trunk=False,
        repair_local_trunk_commit_id=remote_trunk_commit_id,
        trunk_branch=trunk_branch,
        via="push",
    )


def _latest_incomplete_direct_push_land(
    *,
    events: tuple[JournalEvent, ...],
    selected_head_change_id: str,
) -> JournalEvent | None:
    completed_operation_ids = {
        event.operation_id
        for event in events
        if event.operation == "land" and event.event == "completed"
    }
    for event in reversed(events):
        if event.operation != "land" or event.event != "begin":
            continue
        if event.operation_id in completed_operation_ids:
            continue
        scope = event.data.get("resolved_scope", {})
        if not scope.get("push_trunk"):
            continue
        ordered_change_ids = tuple(scope.get("ordered_change_ids", ()))
        if selected_head_change_id in ordered_change_ids:
            return event
    return None


def _land_log_records_applied_trunk_push(
    *,
    events: tuple[JournalEvent, ...],
    operation_id: str,
) -> bool:
    return any(
        event.operation_id == operation_id
        and event.operation == "land"
        and event.event == "mutation_applied"
        and event.data.get("mutation") == "push_trunk"
        for event in events
    )


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


def _logged_retired_change_ids(
    *,
    events: tuple[JournalEvent, ...],
    operation_id: str,
) -> frozenset[str]:
    """Change IDs whose tracking the interrupted land itself already retired."""

    return frozenset(
        str(event.data["change_id"])
        for event in events
        if event.operation_id == operation_id
        and event.event == "saved_state_update"
        and event.data.get("after") is None
    )


def _ensure_logged_land_matches_saved_state(
    *,
    retired_change_ids: frozenset[str],
    revision: LandRevision,
    state: ReviewState,
) -> None:
    cached_change = state.changes.get(revision.change_id)
    if cached_change is None:
        # A missing record is trusted only when this interrupted land's own log
        # proves it finalized the change and retired the tracking before the
        # completed marker was written. Absence for any other reason is
        # ambiguous linkage, which must fail closed.
        if revision.change_id in retired_change_ids:
            return
        raise CliError(
            t"Cannot finish the interrupted land because saved review identity for "
            t"{ui.change_id(revision.change_id)} no longer matches the logged land.",
            hint=t"Run {ui.cmd('view --fetch')} and inspect the stack before retrying.",
        )
    if (
        cached_change.link_state != "active"
        or cached_change.bookmark != revision.bookmark
        or cached_change.pr_number != revision.pull_request_number
    ):
        raise CliError(
            t"Cannot finish the interrupted land because saved review identity for "
            t"{ui.change_id(revision.change_id)} no longer matches the logged land.",
            hint=t"Run {ui.cmd('view --fetch')} and inspect the stack before retrying.",
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
