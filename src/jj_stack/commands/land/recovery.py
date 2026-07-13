"""Reconcile an interrupted direct land before ordinary stack selection."""

from __future__ import annotations

import asyncio

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget, resolve_github_target
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import PendingDirectLand, ReviewState
from jj_stack.state.journal import OperationJournal

from .execute import execute_land_plan
from .models import LandExecutionInputs, LandPlan, LandResult, LandRevision
from .plan import plan_review_bookmark_cleanup_for_revisions


def reconcile_pending_direct_land(
    *,
    context: CommandContext,
    dry_run: bool,
) -> LandResult | None:
    """Reconcile a durable direct-land checkpoint before normal stack selection."""

    state = context.state_store.load()
    pending = state.pending_direct_land
    if pending is None:
        return None

    target = resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(target, GithubTarget):
        message = (
            target.remote_error
            or target.github_repository_error
            or t"Could not determine which Git remote to use."
        )
        raise CliError(message)
    return asyncio.run(
        _reconcile_pending_direct_land_async(
            context=context,
            dry_run=dry_run,
            pending=pending,
            remote=target.remote,
            repository=target.repository,
            state=state,
        )
    )


async def _reconcile_pending_direct_land_async(
    *,
    context: CommandContext,
    dry_run: bool,
    pending: PendingDirectLand,
    remote: GitRemote,
    repository: GithubRepoAddress,
    state: ReviewState,
) -> LandResult | None:
    async with build_github_client(repository=repository) as github_client:
        _ensure_pending_direct_land_scope_matches(
            github_client=github_client,
            pending=pending,
            remote=remote,
            trunk_branch=pending.trunk_branch,
        )
        with console.spinner(description="Refreshing interrupted land state"):
            context.jj_client.fetch_remote(remote=remote.name)
            bookmark_states = context.jj_client.list_bookmark_states()
        plan = await _pending_direct_land_plan(
            bookmark_states=bookmark_states,
            context=context,
            dry_run=dry_run,
            github_client=github_client,
            pending=pending,
            remote=remote,
            state=state,
        )
        if plan is None:
            return None

        bookmark_cleanup_plans = plan_review_bookmark_cleanup_for_revisions(
            bookmark_states=bookmark_states,
            prefix=pending.bookmark_prefix,
            cleanup_bookmarks=pending.cleanup_bookmarks,
            cleanup_user_bookmarks=pending.cleanup_user_bookmarks,
            planned_revisions=plan.planned_revisions,
        )
        selected_revset = pending.planned_revisions[-1].change_id
        trunk_subject = pending.planned_revisions[-1].subject
        if dry_run:
            return LandResult(
                actions=plan.planned_actions(
                    bookmark_cleanup_plans=bookmark_cleanup_plans,
                ),
                applied=False,
                bypass_readiness=False,
                blocked=plan.blocked,
                remote_name=remote.name,
                selected_revset=selected_revset,
                trunk_branch=pending.trunk_branch,
                trunk_subject=trunk_subject,
                via=plan.via,
            )
        return await execute_land_plan(
            bookmark_cleanup_plans=bookmark_cleanup_plans,
            execution=LandExecutionInputs(
                bypass_readiness=False,
                cleanup_bookmarks=pending.cleanup_bookmarks,
                context=context,
                ordered_change_ids=tuple(
                    revision.change_id for revision in pending.planned_revisions
                ),
                ordered_commit_ids=tuple(
                    revision.commit_id for revision in pending.planned_revisions
                ),
                original_trunk_commit_id=pending.original_trunk_commit_id,
                remote_url=pending.remote_url,
                selected_pr_number=None,
            ),
            github_client=github_client,
            merge_method=None,
            plan=plan,
            remote_name=remote.name,
            selected_revset=selected_revset,
            trunk_branch=pending.trunk_branch,
            trunk_subject=trunk_subject,
        )


async def _pending_direct_land_plan(
    *,
    bookmark_states: dict[str, BookmarkState],
    context: CommandContext,
    dry_run: bool,
    github_client: GithubClient,
    pending: PendingDirectLand,
    remote: GitRemote,
    state: ReviewState,
) -> LandPlan | None:
    """Reconcile the one pending direct-land transaction before replanning."""

    trunk_branch = pending.trunk_branch
    trunk_bookmark = bookmark_states.get(trunk_branch)
    if trunk_bookmark is None:
        trunk_bookmark = context.jj_client.get_bookmark_state(trunk_branch)
    remote_trunk = trunk_bookmark.remote_target(remote.name)
    remote_trunk_commit_id = None if remote_trunk is None else remote_trunk.target
    if remote_trunk_commit_id is None:
        raise CliError(
            t"Cannot reconcile the interrupted land because remote trunk "
            t"{ui.bookmark(f'{trunk_branch}@{remote.name}')} is unavailable.",
            hint="Fetch, inspect the remote trunk, and retry.",
        )

    commit_ids = tuple(revision.commit_id for revision in pending.planned_revisions)
    landed_commit_ids = context.jj_client.query_commit_ids_ancestors_of(
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
        if dry_run:
            raise CliError(
                "An earlier direct land did not move remote trunk and needs to be "
                "cleared before a new preview can be computed.",
                hint=t"Run {ui.cmd('land')} without {ui.cmd('--dry-run')} once to reconcile it.",
            )
        _clear_unapplied_pending_direct_land(
            context=context,
            pending=pending,
        )
        return None

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
            or pull_request.head.sha != revision.commit_id
        ):
            raise _pending_direct_land_identity_error(revision.change_id)
        remote_bookmark = bookmark_states.get(
            revision.bookmark,
            BookmarkState(name=revision.bookmark),
        ).remote_target(remote.name)
        if remote_bookmark is not None and len(remote_bookmark.targets) > 1:
            raise _pending_direct_land_identity_error(revision.change_id)
        remote_target = None if remote_bookmark is None else remote_bookmark.target
        if remote_target != revision.commit_id:
            raise _pending_direct_land_identity_error(revision.change_id)
        if revision.change_id in finalized_change_ids and pull_request.state == "open":
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
    context: CommandContext,
    pending: PendingDirectLand,
) -> None:
    """Clear a transaction whose exact commits never reached remote trunk."""

    local_trunk = context.jj_client.get_bookmark_state(pending.trunk_branch)
    if local_trunk.local_target == pending.target_trunk_commit_id:
        if pending.original_local_trunk_commit_id is None:
            context.jj_client.forget_bookmarks((pending.trunk_branch,))
        else:
            context.jj_client.set_bookmark(
                pending.trunk_branch,
                pending.original_local_trunk_commit_id,
                allow_backwards=True,
            )

    state = context.state_store.load()
    if state.pending_direct_land != pending:
        raise CliError("Pending direct-land state changed while it was being reconciled.")
    context.state_store.save(
        state.model_copy(update={"pending_direct_land": None}),
        durable=True,
    )
    OperationJournal.resume(
        context.state_store.state_dir,
        operation="land",
        operation_id=pending.operation_id,
    ).append(
        "completed",
        {"outcome": "trunk_not_moved"},
        durable=True,
    )
