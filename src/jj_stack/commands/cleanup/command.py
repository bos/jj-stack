"""Find and remove unused review artifacts left behind by earlier review work.

By default, this checks the whole repository for saved PR links, review branches, bookmarks, and
comments that no active review still needs.

Open orphaned PRs are left alone. Run `jj-stack list` to see them, then close and clean up one
with `jj-stack unstack --cleanup --pull-request <pr>`, or clean up all of them with
`jj-stack unstack --cleanup --pull-request orphans`.

Use `--dry-run` to preview cleanup without deleting branches, bookmarks, comments, or saved PR
links.

"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.commands._close_actions import (
    BookmarkCleanupPlan,
    BookmarkCleanupRun,
    CloseAction,
    ManagedCommentLookup,
    apply_bookmark_cleanup,
    apply_managed_comment_cleanup,
    authorize_current_review_cleanup,
    authorize_current_tracked_pull_request,
    authorize_tracked_review,
    find_managed_comments,
    native_stack_cleanup_blocker,
    plan_bookmark_cleanup,
)
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.resolution import (
    GithubTarget,
    resolve_github_target,
)
from jj_stack.github.stack_comments import stack_comment_label
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_reviews,
)
from jj_stack.state.operation_lock import (
    acquire_operation_lock,
)
from jj_stack.state.store import ReviewStateStore
from jj_stack.ui import plain_text

from .shared import (
    CleanupAction,
    CleanupResult,
    PreparedCleanup,
    PreparedCleanupChange,
    _build_action_streamer,
    _emit_output_lines,
    _render_cleanup_action_header,
    _render_cleanup_postamble,
)
from .stale import LocalCleanupObservation, _local_cleanup_observations

HELP = "Remove review artifacts that are no longer in use"


@dataclass(frozen=True, slots=True)
class _CleanupMutationRun(BookmarkCleanupRun):
    """Bookmark cleanup mutation context for the plain cleanup command."""

    prepared: PreparedCleanup

    @property
    def dry_run(self) -> bool:
        return self.prepared.dry_run

    @property
    def jj_client(self) -> JjClient:
        return self.prepared.context.jj_client


def cleanup(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="cleanup",
    ):
        return _run_cleanup_command(
            context=context,
            dry_run=dry_run,
        )


def _run_cleanup_command(
    *,
    context: CommandContext,
    dry_run: bool,
) -> int:
    """Render and run the stale cleanup command path."""

    with console.spinner(description="Loading bookmark state"):
        prepared_cleanup = _prepare_cleanup(
            context=context,
            dry_run=dry_run,
        )
    local_observations = _local_cleanup_observations(
        change_ids=tuple(prepared_cleanup.state.review_identities),
        context=prepared_cleanup.context,
    )
    if _cleanup_needs_remote_context(prepared_cleanup=prepared_cleanup):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        for message in github_target_unavailable_messages(prepared_cleanup.github_target):
            console.warning(plain_text(message))

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                header=_render_cleanup_action_header(dry_run=prepared_cleanup.dry_run),
            ),
            prepared_cleanup=prepared_cleanup,
            local_observations=local_observations,
        )
    )
    _emit_output_lines(_render_cleanup_postamble(result=result))
    return 1 if any(action.status == "blocked" for action in result.actions) else 0


def _prepare_cleanup(
    *,
    context: CommandContext,
    dry_run: bool,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    state_store = context.state_store
    state = state_store.load()
    if not dry_run:
        state_store.require_writable()

    bookmark_states = _load_bookmark_states(
        context=context,
        state=state,
    )

    return PreparedCleanup(
        context=context,
        bookmark_states=bookmark_states,
        github_target=None,
        dry_run=dry_run,
        state=state,
    )


async def _run_cleanup_async(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    local_observations: dict[str, LocalCleanupObservation] | None = None,
) -> CleanupResult:
    recorder = ActionRecorder[CleanupAction](on_action=on_action)
    if local_observations is None:
        local_observations = _local_cleanup_observations(
            change_ids=tuple(prepared_cleanup.state.review_identities),
            context=prepared_cleanup.context,
        )
    if _cleanup_needs_remote_context(prepared_cleanup=prepared_cleanup):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
    if isinstance(prepared_cleanup.github_target, GithubTarget):
        prepared_cleanup = _refresh_review_branches(
            prepared_cleanup=prepared_cleanup,
        )
    prepared_changes = _run_local_cleanup_pass(
        prepared_cleanup=prepared_cleanup,
        record_action=recorder.record,
        local_observations=local_observations,
    )
    github_target = prepared_cleanup.github_target
    if isinstance(github_target, GithubTarget) and prepared_changes:
        async with build_github_client(repository=github_target.repository) as github_client:
            await _run_tracked_review_cleanup_pass(
                github_client=github_client,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                record_action=recorder.record,
            )
    elif prepared_changes:
        for prepared_change in prepared_changes:
            recorder.record(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect PR #{prepared_change.review_identity.pr_number} for "
                    t"{ui.change_id(prepared_change.change_id)} because the GitHub repository "
                    t"cannot be resolved",
                )
            )
    return CleanupResult(actions=recorder.as_tuple())


def _run_local_cleanup_pass(
    *,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    local_observations: dict[str, LocalCleanupObservation],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    for change_id, review_identity in prepared_cleanup.state.review_identities.items():
        submitted_baseline = prepared_cleanup.state.submitted_baselines.get(change_id)
        if submitted_baseline is None:
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="skipped",
                    body=t"leave {ui.change_id(change_id)} tracked because its last submitted "
                    t"commit is unavailable; run {ui.cmd('relink')} to repair PR tracking",
                )
            )
            continue
        local_observation = local_observations.get(
            change_id,
            LocalCleanupObservation(
                current_commit_id=None,
                stale_reason="local change was not inspected",
            ),
        )
        bookmark_state = prepared_cleanup.bookmark_states.get(
            review_identity.head_ref,
            BookmarkState(name=review_identity.head_ref),
        )
        prepared_change = PreparedCleanupChange(
            bookmark_state=bookmark_state,
            change_id=change_id,
            current_commit_id=local_observation.current_commit_id,
            review_identity=review_identity,
            stale_reason=local_observation.stale_reason,
            submitted_baseline=submitted_baseline,
        )
        prepared_changes.append(prepared_change)
    return tuple(prepared_changes)


def _refresh_review_branches(
    *,
    prepared_cleanup: PreparedCleanup,
) -> PreparedCleanup:
    """Fetch only exact saved branches that cleanup may inspect."""

    remote = prepared_cleanup.remote
    github_target = prepared_cleanup.github_target
    if remote is None or not isinstance(github_target, GithubTarget):
        return prepared_cleanup
    branches = tuple(
        identity.head_ref
        for change_id, identity in prepared_cleanup.state.review_identities.items()
        if change_id in prepared_cleanup.state.submitted_baselines
        and identity.repository_key == github_target.repository.repository_key
    )
    if not branches:
        return prepared_cleanup
    prepared_cleanup.context.jj_client.fetch_remote(
        remote=remote.name,
        branches=branches,
    )
    return replace(
        prepared_cleanup,
        bookmark_states=_load_bookmark_states(
            context=prepared_cleanup.context,
            state=prepared_cleanup.state,
        ),
    )


async def _run_tracked_review_cleanup_pass(
    *,
    github_client: GithubClient,
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Clean closed exact review records while preserving open or ambiguous ones."""

    if not prepared_changes:
        return
    observations = await _observe_cleanup_reviews(
        change_ids=tuple(change.change_id for change in prepared_changes),
        github_client=github_client,
        state_store=prepared_cleanup.context.state_store,
    )
    for prepared_change in prepared_changes:
        observation = observations[prepared_change.change_id]
        if isinstance(observation, GithubClientError):
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect saved PR "
                    t"#{prepared_change.review_identity.pr_number} for "
                    t"{ui.change_id(prepared_change.change_id)}; fix GitHub access, then rerun "
                    t"{ui.cmd('cleanup')}",
                )
            )
            continue
        await _cleanup_tracked_review(
            github_client=github_client,
            initial_observation=observation,
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )


async def _observe_cleanup_reviews(
    *,
    change_ids: tuple[str, ...],
    github_client: GithubClient,
    state_store: ReviewStateStore,
) -> dict[str, RepositoryObservation | GithubClientError]:
    """Batch first, then isolate only records that GitHub cannot decode."""

    async def observe_one(change_id: str) -> RepositoryObservation | GithubClientError:
        try:
            return await observe_reviews(
                change_ids=(change_id,),
                github_client=github_client,
                include_open_dependents=True,
                state_store=state_store,
            )
        except GithubClientError as error:
            return error

    try:
        observation = await observe_reviews(
            change_ids=change_ids,
            github_client=github_client,
            include_open_dependents=True,
            state_store=state_store,
        )
    except GithubClientError:
        values = await run_bounded_tasks(
            concurrency=DEFAULT_BOUNDED_CONCURRENCY,
            items=change_ids,
            run_item=observe_one,
        )
    else:
        values = (observation,) * len(change_ids)
    return dict(zip(change_ids, values, strict=True))


async def _cleanup_tracked_review(
    *,
    github_client: GithubClient,
    initial_observation: RepositoryObservation,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Plan and apply cleanup for one exact closed review."""

    identity = prepared_change.review_identity
    authorized, authorization_action = _review_cleanup_authorization(
        observation=initial_observation,
        prepared_change=prepared_change,
    )
    if not authorized:
        if authorization_action is not None:
            record_action(authorization_action)
        return

    cleanup_plan, cleanup_actions = _plan_tracked_review_cleanup(
        bookmark_state=prepared_change.bookmark_state,
        prepared_change=prepared_change,
        prepared_cleanup=prepared_cleanup,
    )
    for action in cleanup_actions:
        record_action(_cleanup_action(action))
    if cleanup_plan.blocked:
        return
    native_blocker = await native_stack_cleanup_blocker(
        delete_remote_branch=cleanup_plan.remote_delete,
        github_client=github_client,
        persist=not prepared_cleanup.dry_run,
        pull_number=identity.pr_number,
        state_store=prepared_cleanup.context.state_store,
    )
    if native_blocker is not None:
        record_action(_cleanup_action(native_blocker))
        return
    comment_lookups = await _preflight_cleanup_comments(
        github_client=github_client,
        identity=identity,
        record_action=record_action,
    )
    if comment_lookups is None:
        return
    if prepared_cleanup.dry_run:
        await _apply_tracked_review_cleanup(
            cleanup_plan=cleanup_plan,
            comment_lookups=comment_lookups,
            github_client=github_client,
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        return

    mutation_blocker = await authorize_current_review_cleanup(
        change_id=prepared_change.change_id,
        delete_remote_branch=cleanup_plan.remote_delete,
        github_client=github_client,
        review_identity=prepared_change.review_identity,
        state_store=prepared_cleanup.context.state_store,
        submitted_baseline=prepared_change.submitted_baseline,
    )
    if mutation_blocker is not None:
        record_action(_cleanup_action(mutation_blocker))
        return
    await _apply_tracked_review_cleanup(
        cleanup_plan=cleanup_plan,
        comment_lookups=comment_lookups,
        github_client=github_client,
        prepared_change=prepared_change,
        prepared_cleanup=prepared_cleanup,
        record_action=record_action,
    )


def _plan_tracked_review_cleanup(
    *,
    bookmark_state: BookmarkState,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
) -> tuple[BookmarkCleanupPlan, tuple[CloseAction, ...]]:
    """Plan branch and local-bookmark cleanup from one fresh observation."""

    identity = prepared_change.review_identity
    baseline = prepared_change.submitted_baseline
    actions: list[CloseAction] = []
    plan = plan_bookmark_cleanup(
        bookmark=identity.head_ref,
        bookmark_state=bookmark_state,
        change_id=prepared_change.change_id,
        local_commit_id=prepared_change.current_commit_id,
        record_action=actions.append,
        remote_name=None if prepared_cleanup.remote is None else prepared_cleanup.remote.name,
        remote_commit_id=baseline.commit_id,
        review_identity=identity,
    )
    return plan, tuple(actions)


async def _preflight_cleanup_comments(
    *,
    github_client: GithubClient,
    identity: ReviewIdentity,
    record_action: Callable[[CleanupAction], None],
) -> tuple[ManagedCommentLookup, ...] | None:
    """Resolve managed comments, recording and stopping on ambiguous lookup."""

    lookups = await find_managed_comments(
        github_client=github_client,
        pull_request_number=identity.pr_number,
    )
    for lookup in lookups:
        if lookup.blocked_reason is None:
            continue
        record_action(
            CleanupAction(
                kind=stack_comment_label(lookup.kind),
                status="blocked",
                body=lookup.blocked_reason,
            )
        )
        return None
    return lookups


async def _apply_tracked_review_cleanup(
    *,
    cleanup_plan: BookmarkCleanupPlan,
    comment_lookups: tuple[ManagedCommentLookup, ...],
    github_client: GithubClient,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> bool:
    """Apply an authorized branch/comment cleanup and retire the exact pair."""

    identity = prepared_change.review_identity
    baseline = prepared_change.submitted_baseline
    run = _CleanupMutationRun(prepared_cleanup)
    cleanup_current = apply_bookmark_cleanup(
        bookmark=identity.head_ref,
        change_id=prepared_change.change_id,
        cleanup_plan=cleanup_plan,
        local_commit_id=prepared_change.current_commit_id,
        record_action=lambda action: record_action(_cleanup_action(action)),
        remote_name=None if prepared_cleanup.remote is None else prepared_cleanup.remote.name,
        remote_commit_id=baseline.commit_id,
        review_identity=identity,
        run=run,
    )
    if not cleanup_current:
        return False
    comment_actions, comments_current = await apply_managed_comment_cleanup(
        change_id=prepared_change.change_id,
        dry_run=prepared_cleanup.dry_run,
        github_client=github_client,
        lookups=comment_lookups,
        review_identity=identity,
        state_store=prepared_cleanup.context.state_store,
        submitted_baseline=baseline,
    )
    for action in comment_actions:
        record_action(_cleanup_action(action))
    if not comments_current:
        return False
    if not prepared_cleanup.dry_run:
        _pull_request, blocker = await authorize_current_tracked_pull_request(
            allowed_states=frozenset({"closed", "merged"}),
            change_id=prepared_change.change_id,
            github_client=github_client,
            require_no_open_dependents=True,
            review_identity=identity,
            state_store=prepared_cleanup.context.state_store,
            submitted_baseline=baseline,
        )
        if blocker is not None:
            record_action(_cleanup_action(blocker))
            return False
    reason = (
        f" ({prepared_change.stale_reason})" if prepared_change.stale_reason is not None else ""
    )
    record_action(
        CleanupAction(
            kind="tracking",
            status="planned" if prepared_cleanup.dry_run else "applied",
            body=t"remove tracking for {ui.change_id(prepared_change.change_id)}{reason}",
        )
    )
    if not prepared_cleanup.dry_run:
        prepared_cleanup.context.state_store.retire_review(
            prepared_change.change_id,
            expected_identity=identity,
            expected_baseline=baseline,
        )
    return True


def _review_cleanup_authorization(
    *,
    observation: RepositoryObservation,
    prepared_change: PreparedCleanupChange,
) -> tuple[bool, CleanupAction | None]:
    """Observe whether the exact saved pull request is closed and cleanable."""

    identity = prepared_change.review_identity
    observed = observation.reviews[prepared_change.change_id].pull_request
    pull_request, blocker = authorize_tracked_review(
        allowed_states=frozenset({"open", "closed", "merged"}),
        change_id=prepared_change.change_id,
        observation=observation,
        require_no_open_dependents=observed is None or observed.normalize_state().state != "open",
        review_identity=identity,
        submitted_baseline=prepared_change.submitted_baseline,
    )
    if blocker is not None:
        return False, _cleanup_action(blocker)
    if pull_request is None:
        raise AssertionError("Exact cleanup lookup must return a pull request.")
    if pull_request.state == "open":
        return (
            False,
            (
                CleanupAction(
                    kind="tracking",
                    status="skipped",
                    body=t"preserve open orphan PR #{identity.pr_number}",
                )
                if prepared_change.stale_reason is not None
                else None
            ),
        )
    return True, None


def _cleanup_action(action: CloseAction) -> CleanupAction:
    return CleanupAction(kind=action.kind, status=action.status, body=action.body)


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.github_target is not None:
        return prepared_cleanup
    return replace(
        prepared_cleanup,
        github_target=resolve_github_target(
            prepared_cleanup.context.jj_client.list_git_remotes()
        ),
    )


def _cleanup_needs_remote_context(
    *,
    prepared_cleanup: PreparedCleanup,
) -> bool:
    """Whether plain cleanup might need remote or GitHub state beyond local checks."""

    return any(
        change_id in prepared_cleanup.state.submitted_baselines
        for change_id in prepared_cleanup.state.review_identities
    )


def _load_bookmark_states(
    *,
    context: CommandContext,
    state: ReviewState,
) -> dict[str, BookmarkState]:
    jj_client = context.jj_client
    bookmark_states = jj_client.list_bookmark_states()
    tracked_bookmarks = {
        review_identity.head_ref
        for change_id, review_identity in state.review_identities.items()
        if change_id in state.submitted_baselines
    }
    if not tracked_bookmarks:
        return {}

    filtered = {
        bookmark: bookmark_states[bookmark]
        for bookmark in tracked_bookmarks
        if bookmark in bookmark_states
    }
    for bookmark in tracked_bookmarks:
        filtered.setdefault(bookmark, BookmarkState(name=bookmark))
    return filtered
