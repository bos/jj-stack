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
from jj_stack.errors import CliError, DriftError, UsageError
from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import (
    resolve_trunk_branch,
)
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.github import GithubPullRequest, GithubRepository
from jj_stack.models.review_state import PendingDirectLand, ReviewState
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
from jj_stack.state.journal import OperationJournal
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


class _ReprepareLand(Exception):
    """The pending transaction changed local state used by preparation."""


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
    try:
        result = _stream_land(prepared_land=prepared_land)
    except _ReprepareLand:
        with console.spinner(description="Re-inspecting jj stack"):
            prepared_land = _prepare_land(
                bypass_readiness=prepared_land.bypass_readiness,
                cleanup_bookmarks=prepared_land.cleanup_bookmarks,
                context=prepared_land.context,
                dry_run=prepared_land.dry_run,
                merge_method=prepared_land.merge_method,
                revset=prepared_land.prepared_status.selected_revset,
                selected_pr_number=prepared_land.selected_pr_number,
                via=prepared_land.via,
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
                prefix=(
                    prepared_land.context.config.bookmark_prefix
                    if plan.bookmark_prefix is None
                    else plan.bookmark_prefix
                ),
                cleanup_bookmarks=(
                    prepared_land.cleanup_bookmarks
                    if plan.cleanup_bookmarks is None
                    else plan.cleanup_bookmarks
                ),
                cleanup_user_bookmarks=(
                    prepared_land.context.config.cleanup_user_bookmarks
                    if plan.cleanup_user_bookmarks is None
                    else plan.cleanup_user_bookmarks
                ),
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

        completion_plan = await _pending_direct_land_plan(
            bookmark_states=bookmark_states,
            github_client=github_client,
            prepared_land=prepared_land,
            remote=remote,
            trunk_branch=trunk_branch,
        )
        if completion_plan is not None:
            return await finish_plan(completion_plan)

        selected_stack_is_off_trunk = (
            bool(prepared.stack.revisions)
            and prepared.stack.base_parent.commit_id != prepared.stack.trunk.commit_id
        )
        if selected_stack_is_off_trunk:
            raise _stack_not_on_trunk_error(
                prepared_status=prepared_status,
                status_result=status_result,
            )

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


async def _pending_direct_land_plan(
    *,
    bookmark_states: dict[str, BookmarkState],
    github_client: GithubClient,
    prepared_land: PreparedLand,
    remote: GitRemote,
    trunk_branch: str,
) -> LandPlan | None:
    """Reconcile the one pending direct-land transaction before replanning."""

    prepared = prepared_land.prepared_status.prepared
    state = prepared.state_store.load()
    pending = state.pending_direct_land
    if pending is None:
        return None

    _ensure_pending_direct_land_scope_matches(
        github_client=github_client,
        pending=pending,
        remote=remote,
        trunk_branch=trunk_branch,
    )
    trunk_bookmark = bookmark_states.get(trunk_branch)
    if trunk_bookmark is None:
        trunk_bookmark = prepared.client.get_bookmark_state(trunk_branch)
    remote_trunk = trunk_bookmark.remote_target(remote.name)
    remote_trunk_commit_id = None if remote_trunk is None else remote_trunk.target
    if remote_trunk_commit_id is None:
        raise CliError(
            t"Cannot reconcile the interrupted land because remote trunk "
            t"{ui.bookmark(f'{trunk_branch}@{remote.name}')} is unavailable.",
            hint="Fetch, inspect the remote trunk, and retry.",
        )

    commit_ids = tuple(
        revision.commit_id for revision in pending.planned_revisions
    )
    landed_commit_ids = prepared.client.query_commit_ids_ancestors_of(
        commit_ids,
        descendant_commit_id=remote_trunk_commit_id,
    )
    if set(commit_ids) - landed_commit_ids:
        if pending.phase == "trunk_moved":
            raise CliError(
                t"Cannot finish the interrupted land because its exact commits are no "
                t"longer all on {ui.revset('trunk()')}.",
                hint="Inspect the current trunk and the pending land before retrying.",
            )
        if prepared_land.dry_run:
            raise CliError(
                "An earlier direct land did not move remote trunk and needs to be "
                "cleared before a new preview can be computed.",
                hint=t"Run {ui.cmd('land')} without {ui.cmd('--dry-run')} once to "
                t"reconcile it.",
            )
        _clear_unapplied_pending_direct_land(
            pending=pending,
            prepared_land=prepared_land,
        )
        raise _ReprepareLand

    await _ensure_pending_direct_land_review_identities(
        bookmark_states=bookmark_states,
        github_client=github_client,
        pending=pending,
        remote=remote,
        state=state,
    )
    return LandPlan(
        blocked=False,
        bookmark_prefix=pending.bookmark_prefix,
        boundary_action=None,
        cleanup_bookmarks=pending.cleanup_bookmarks,
        cleanup_user_bookmarks=pending.cleanup_user_bookmarks,
        planned_revisions=tuple(
            LandRevision(
                bookmark=revision.bookmark,
                bookmark_managed=revision.bookmark_ownership == "managed",
                change_id=revision.change_id,
                commit_id=revision.commit_id,
                needs_resubmit=False,
                pull_request_number=revision.pull_request_number,
                subject=revision.subject,
            )
            for revision in pending.planned_revisions
        ),
        push_trunk=False,
        repair_local_trunk_commit_id=remote_trunk_commit_id,
        resume_pending_direct_land=True,
        trunk_branch=trunk_branch,
        via="push",
    )


def _ensure_pending_direct_land_scope_matches(
    *,
    github_client: GithubClient,
    pending: PendingDirectLand,
    remote: GitRemote,
    trunk_branch: str,
) -> None:
    expected_scope = (
        pending.github_host,
        pending.github_repository,
        pending.remote_name,
        pending.remote_url,
        pending.trunk_branch,
    )
    current_scope = (
        github_client.repository.host,
        github_client.repository.full_name,
        remote.name,
        remote.url,
        trunk_branch,
    )
    if current_scope == expected_scope:
        return
    raise CliError(
        "Cannot finish the interrupted land because the GitHub repository, remote, "
        "or trunk branch changed since it began.",
        hint="Restore the original target or inspect and clear the pending land manually.",
    )


async def _ensure_pending_direct_land_review_identities(
    *,
    bookmark_states: dict[str, BookmarkState],
    github_client: GithubClient,
    pending: PendingDirectLand,
    remote: GitRemote,
    state: ReviewState,
) -> None:
    finalized_change_ids = set(pending.finalized_change_ids)
    pull_requests = await asyncio.gather(
        *(
            _load_pending_pull_request(
                github_client=github_client,
                pull_request_number=revision.pull_request_number,
            )
            for revision in pending.planned_revisions
        )
    )
    for revision, pull_request in zip(
        pending.planned_revisions,
        pull_requests,
        strict=True,
    ):
        cached_change = state.changes.get(revision.change_id)
        if (
            cached_change is None
            or cached_change.link_state != "active"
            or cached_change.bookmark != revision.bookmark
            or cached_change.pr_number != revision.pull_request_number
            or cached_change.bookmark_ownership != revision.bookmark_ownership
        ):
            raise _pending_direct_land_identity_error(revision.change_id)

        expected_head_label = f"{github_client.repository.owner}:{revision.bookmark}"
        if (
            pull_request.head.ref != revision.bookmark
            or pull_request.head.label != expected_head_label
        ):
            raise _pending_direct_land_identity_error(revision.change_id)
        remote_bookmark = bookmark_states.get(
            revision.bookmark,
            BookmarkState(name=revision.bookmark),
        ).remote_target(remote.name)
        if remote_bookmark is not None and len(remote_bookmark.targets) > 1:
            raise _pending_direct_land_identity_error(revision.change_id)
        remote_target = None if remote_bookmark is None else remote_bookmark.target
        if remote_target is not None and remote_target != revision.commit_id:
            raise _pending_direct_land_identity_error(revision.change_id)
        if pull_request.state == "open" and remote_target != revision.commit_id:
            raise _pending_direct_land_identity_error(revision.change_id)
        if (
            revision.change_id in finalized_change_ids
            and pull_request.state == "open"
        ):
            raise _pending_direct_land_identity_error(revision.change_id)


async def _load_pending_pull_request(
    *,
    github_client: GithubClient,
    pull_request_number: int,
) -> GithubPullRequest:
    try:
        pull_request = await github_client.get_pull_request(
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not verify PR #{pull_request_number} for the interrupted land"
        ) from error
    return pull_request.normalize_state()


def _pending_direct_land_identity_error(change_id: str) -> CliError:
    return CliError(
        t"Cannot finish the interrupted land because review identity for "
        t"{ui.change_id(change_id)} changed after trunk moved.",
        hint=t"Run {ui.cmd('view --fetch')} and inspect the stack before retrying.",
    )


def _clear_unapplied_pending_direct_land(
    *,
    pending: PendingDirectLand,
    prepared_land: PreparedLand,
) -> None:
    """Clear a transaction whose exact commits never reached remote trunk."""

    prepared = prepared_land.prepared_status.prepared
    local_trunk = prepared.client.get_bookmark_state(pending.trunk_branch)
    if local_trunk.local_target == pending.target_trunk_commit_id:
        prepared.client.set_bookmark(
            pending.trunk_branch,
            pending.original_trunk_commit_id,
            allow_backwards=True,
        )

    state = prepared.state_store.load()
    if state.pending_direct_land != pending:
        raise CliError("Pending direct-land state changed while it was being reconciled.")
    prepared.state_store.save(
        state.model_copy(update={"pending_direct_land": None}),
        durable=True,
    )
    OperationJournal.resume(
        prepared.state_store.state_dir,
        operation="land",
        operation_id=pending.operation_id,
    ).append(
        "completed",
        {"outcome": "trunk_not_moved"},
        durable=True,
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
                t"Some lower changes from this stack already landed. Run "
                t"{ui.cmd('cleanup --rebase')} {ui.revset(status_result.selected_revset)} "
                t"to rebase the remaining local changes before retrying."
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
