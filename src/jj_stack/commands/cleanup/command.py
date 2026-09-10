"""Clean up closed or merged pull requests.

This removes unused PR branches, stack overview comments, and saved pull request links. It keeps
your local changes and other comments on GitHub. For merged PRs, run `jj-stack sync` first to
update the local stack.

With no selector, it checks the whole repo. A revset limits cleanup to one local stack;
`--pull-request` selects one tracked pull request, and `--pull-request orphans` selects every
tracked pull request whose local change is gone. Add `--close` to a `--pull-request` selection
to retarget those open pull requests to trunk and close them before cleanup. To close a whole
stack, first run `jj-stack unstack`, then close each PR from the top of the stack downward.

Without `--close`, open pull requests are left alone.

Cleanup keeps a branch while another open or reopenable closed PR uses it as a base, or an
unmerged PR in a GitHub stack needs it. The message names the PR or stack to update before
retrying cleanup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.cleanup.actions import (
    CleanupAction,
    CleanupResult,
    apply_overview_comment_cleanup,
    apply_remote_branch_cleanup,
    check_tracked_pr,
    close_pr_on_trunk,
    github_stack_cleanup_blockers,
    plan_pr_cleanup,
)
from jj_stack.errors import (
    AmbiguousSelectionError,
    CliError,
    UsageError,
    error_hint,
    error_message,
)
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.github.resolution import GithubTarget, UnresolvedGithubTarget, resolve_github_target
from jj_stack.identifiers import ChangeId, short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import PRRefUpdate
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubIssueComment, GithubPR, GithubStack
from jj_stack.models.tracking import TrackedPR, TrackingState
from jj_stack.stack.change_state import enumerate_orphaned_records
from jj_stack.stack.pr_facts import (
    RepoFacts,
    observe_github_stacks,
    observe_prs,
)
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.selected import select_stack_path
from jj_stack.stack.selection import resolve_pr_reference
from jj_stack.stack.trunk import observe_trunk_branch
from jj_stack.state.operation_lock import operation_lock
from jj_stack.ui import plain_text

HELP = "Remove unused PR branches, stack overviews, and saved links"


@dataclass(frozen=True, slots=True)
class PreparedCleanup:
    """Locally prepared cleanup inputs before any GitHub inspection."""

    candidates: dict[ChangeId, TrackedPR]
    close_open_prs: bool
    context: CommandContext
    dry_run: bool
    github_target: GithubTarget | UnresolvedGithubTarget
    state: TrackingState


@dataclass(frozen=True, slots=True)
class PRCleanup:
    """One eligible PR, its branch deletion, and an optional close after retargeting."""

    pr: GithubPR
    update: PRRefUpdate | None
    close_to: str | None = None


type CleanupPreflight = PRCleanup | CleanupAction | None


def _build_action_streamer(*, header: str) -> Callable[[CleanupAction], None]:
    """Print the action header once, then stream actions as they arrive."""

    header_printed = False

    def emit_action(action: CleanupAction) -> None:
        nonlocal header_printed
        if not header_printed:
            console.output(header)
            header_printed = True
        console.action_row(
            kind=None if action.kind == "tracking" else action.kind,
            status=action.status,
            body=action.body,
        )

    return emit_action


def cleanup(
    *,
    cli_args: JjCliArgs,
    close: bool,
    debug: bool,
    dry_run: bool,
    pr: str | None,
    repo: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    if pr is not None and revset is not None:
        raise UsageError("jj-stack cleanup --pull-request cannot be combined with a revset.")
    if close and pr is None:
        raise UsageError("jj-stack cleanup --close requires --pull-request.")

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    with operation_lock(
        context.state_store,
        command="cleanup",
        mutating=not dry_run,
    ):
        return _run_cleanup_command(
            close=close,
            context=context,
            dry_run=dry_run,
            pr=pr,
            revset=revset,
        )


def _run_cleanup_command(
    *,
    close: bool,
    context: CommandContext,
    dry_run: bool,
    pr: str | None,
    revset: str | None,
) -> int:
    """Render and run cleanup for the selected pull requests."""

    with console.spinner(description="Loading PR state"):
        prepared_cleanup = _prepare_cleanup(
            close=close,
            context=context,
            dry_run=dry_run,
            pr=pr,
            revset=revset,
        )
    if prepared_cleanup.candidates:
        for message in github_target_unavailable_messages(prepared_cleanup.github_target):
            console.warning(plain_text(message))

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                header=("Cleanup preview:" if prepared_cleanup.dry_run else "Cleanup:"),
            ),
            prepared_cleanup=prepared_cleanup,
        )
    )
    if not result.actions:
        console.output("No cleanup actions needed.")
    return 1 if any(action.status == "blocked" for action in result.actions) else 0


async def cleanup_tracked_prs(
    *,
    change_ids: tuple[ChangeId, ...],
    context: CommandContext,
    dry_run: bool,
    github_client: GithubClient,
    github_target: GithubTarget,
    planned_detached_dependents: frozenset[int] = frozenset(),
    planned_local_removals: frozenset[ChangeId] = frozenset(),
) -> CleanupResult:
    """Run cleanup for PRs reconciled by another command."""

    state = context.state_store.load()
    prepared_cleanup = PreparedCleanup(
        candidates=_cleanup_candidates(state, change_ids),
        close_open_prs=False,
        context=context,
        dry_run=dry_run,
        github_target=github_target,
        state=state,
    )

    def retry_hint() -> ui.Message:
        remaining = context.state_store.load().prs
        commands = tuple(
            f"jj-stack cleanup --pull-request {tracked.pr_identity.pr_number}"
            for change_id in change_ids
            if (tracked := remaining.get(change_id)) is not None
        )
        if not commands:
            return t"Inspect remaining work with {ui.cmd('jj-stack list')}."
        return t"Finish cleanup with {ui.join(ui.cmd, commands)}."

    try:
        result = await _run_cleanup_async(
            github_client=github_client,
            on_action=_build_action_streamer(
                header="Cleanup preview:" if dry_run else "Cleanup:",
            ),
            prepared_cleanup=prepared_cleanup,
            preview_detached_dependents=(planned_detached_dependents if dry_run else frozenset()),
            preview_local_removals=(planned_local_removals if dry_run else frozenset()),
        )
    except (CliError, GithubClientError) as error:
        if error_hint(error) is not None:
            raise
        raise CliError(error_message(error), hint=retry_hint()) from error
    if any(action.status == "blocked" for action in result.actions):
        console.note(retry_hint())
    return result


def _prepare_cleanup(
    *,
    close: bool,
    context: CommandContext,
    dry_run: bool,
    pr: str | None,
    revset: str | None,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    state_store = context.state_store
    state = state_store.load()
    selected_change_ids = _resolve_cleanup_change_ids(
        context=context,
        pr=pr,
        revset=revset,
        state=state,
    )

    return PreparedCleanup(
        candidates=_cleanup_candidates(
            state, state.prs if selected_change_ids is None else selected_change_ids
        ),
        close_open_prs=close,
        context=context,
        dry_run=dry_run,
        github_target=resolve_github_target(context.jj_client.list_git_remotes()),
        state=state,
    )


def _cleanup_candidates(
    state: TrackingState, change_ids: Iterable[ChangeId]
) -> dict[ChangeId, TrackedPR]:
    return {
        change_id: tracked
        for change_id in change_ids
        if (tracked := state.prs.get(change_id)) is not None
    }


def _resolve_cleanup_change_ids(
    *,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
    state: TrackingState,
) -> tuple[ChangeId, ...] | None:
    """Resolve an optional cleanup selector to saved change IDs."""

    if pr == "orphans":
        repo_paths = observe_repo_paths(
            jj_client=context.jj_client,
            state=state,
        )
        tracked_stacks = tuple(path.stack for path in repo_paths.paths if path.tracked_change_ids)
        return tuple(
            orphan.change_id for orphan in enumerate_orphaned_records(state, tracked_stacks)
        )
    if pr is not None:
        pr_number, repo = resolve_pr_reference(
            jj_client=context.jj_client,
            pr_reference=pr,
        )
        matches = tuple(
            change_id
            for change_id, tracked in state.prs.items()
            if tracked.pr_identity.pr_number == pr_number
        )
        if len(matches) > 1:
            pr_label = format_pr_label(pr_number, repo=repo)
            raise AmbiguousSelectionError(
                t"Multiple saved links claim {pr_label}.",
                hint=t"Run {ui.cmd('jj-stack list')} to inspect them and repair the incorrect "
                t"link.",
            )
        if not matches:
            pr_label = format_pr_label(pr_number, repo=repo)
            raise CliError(
                t"{pr_label} is not linked to any local change.",
                hint=t"For an open PR, save its link with "
                t"{ui.cmd(f'jj-stack checkout --pull-request {pr_number}')}. "
                t"To close it without jj-stack, run {ui.cmd(f'gh pr close {pr_number}')}.",
            )
        return matches
    if revset is None:
        return None
    stack = select_stack_path(
        jj_client=context.jj_client,
        revset=revset,
        state=state,
    ).stack
    return tuple(change.change_id for change in stack.changes if change.change_id in state.prs)


async def _run_cleanup_async(
    *,
    github_client: GithubClient | None = None,
    on_action: Callable[[CleanupAction], None],
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int] = frozenset(),
    preview_local_removals: frozenset[ChangeId] = frozenset(),
) -> CleanupResult:
    actions: list[CleanupAction] = []

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        on_action(action)

    candidates = prepared_cleanup.candidates
    github_target = prepared_cleanup.github_target
    if isinstance(github_target, GithubTarget) and candidates:
        if github_client is not None:
            await _run_tracked_pr_cleanup_pass(
                github_client=github_client,
                candidates=candidates,
                prepared_cleanup=prepared_cleanup,
                preview_detached_dependents=preview_detached_dependents,
                preview_local_removals=preview_local_removals,
                record_action=record_action,
                remote=github_target.remote,
            )
        else:
            async with prepared_cleanup.context.open_github_client(
                repo=github_target.repo
            ) as client:
                await _run_tracked_pr_cleanup_pass(
                    github_client=client,
                    candidates=candidates,
                    prepared_cleanup=prepared_cleanup,
                    preview_detached_dependents=preview_detached_dependents,
                    preview_local_removals=preview_local_removals,
                    record_action=record_action,
                    remote=github_target.remote,
                )
    elif candidates:
        for change_id, candidate in candidates.items():
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect PR #{candidate.pr_identity.pr_number} for "
                    t"{ui.change_id(change_id)} because the GitHub repo "
                    t"cannot be resolved",
                )
            )
    return CleanupResult(actions=tuple(actions))


async def _run_tracked_pr_cleanup_pass(
    *,
    github_client: GithubClient,
    candidates: Mapping[ChangeId, TrackedPR],
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int] = frozenset(),
    preview_local_removals: frozenset[ChangeId] = frozenset(),
    record_action: Callable[[CleanupAction], None],
    remote: GitRemote,
) -> None:
    """Clean up closed PRs, skipping open PRs and ambiguous links."""

    remote_name = remote.name
    observation = await observe_prs(
        change_ids=tuple(candidates),
        context=prepared_cleanup.context,
        github_client=github_client,
        include_dependents=True,
        include_open_head_prs=True,
        remote_name=remote_name,
        state=prepared_cleanup.state,
    )
    preflights: dict[ChangeId, CleanupPreflight] = {}
    eligible_pr_numbers: list[int] = []
    for change_id, candidate in candidates.items():
        preflight = _preflight_tracked_pr_cleanup(
            initial_observation=observation,
            change_id=change_id,
            prepared_cleanup=prepared_cleanup,
            preview_detached_dependents=preview_detached_dependents,
            preview_local_removals=preview_local_removals,
        )
        preflights[change_id] = preflight
        if isinstance(preflight, PRCleanup):
            eligible_pr_numbers.append(candidate.pr_identity.pr_number)
    open_plans = {
        change_id: preflight
        for change_id, preflight in preflights.items()
        if isinstance(preflight, PRCleanup) and preflight.pr.state == "open"
    }
    if open_plans:
        jj_client = prepared_cleanup.context.jj_client
        trunk_commit_id = jj_client.resolve_commit("trunk()").commit_id
        trunk_branch, _targets = observe_trunk_branch(
            jj_client=jj_client,
            github_repo_state=observation.github_repo,
            remote=remote,
            trunk_commit_id=trunk_commit_id,
        )
        preflights.update(
            (change_id, replace(plan, close_to=trunk_branch))
            for change_id, plan in open_plans.items()
        )
    stacks, overview_comments = await _observe_cleanup_secondary_facts(
        github_client=github_client,
        pr_numbers=eligible_pr_numbers,
    )
    stack_blockers = github_stack_cleanup_blockers(
        pr_numbers=tuple(eligible_pr_numbers),
        stacks=stacks,
    )
    for change_id, candidate in candidates.items():
        stop_after_failure = await _cleanup_tracked_pr(
            github_client=github_client,
            preflight=preflights[change_id],
            candidate=candidate,
            change_id=change_id,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
            remote_name=remote_name,
            stack_blocker=stack_blockers.get(candidate.pr_identity.pr_number),
            overview_comments=overview_comments,
        )
        if stop_after_failure:
            break


async def _observe_cleanup_secondary_facts(
    *,
    github_client: GithubClient,
    pr_numbers: list[int],
) -> tuple[tuple[GithubStack, ...] | CliError, dict[int, GithubIssueComment | None]]:
    """Join the two shared secondary observations with stack errors taking precedence."""

    if not pr_numbers:
        return (), {}
    stacks_task = asyncio.create_task(observe_github_stacks(github=github_client))
    comments_task = asyncio.create_task(
        github_client.find_issue_comments_by_body_marker(
            body_marker=STACK_OVERVIEW_COMMENT_MARKER,
            pr_numbers=pr_numbers,
        )
    )
    await asyncio.gather(stacks_task, comments_task, return_exceptions=True)
    try:
        stacks = await stacks_task
    except CliError as error:
        return error, {}
    return stacks, await comments_task


async def _cleanup_tracked_pr(
    *,
    github_client: GithubClient,
    preflight: CleanupPreflight,
    candidate: TrackedPR,
    change_id: ChangeId,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
    stack_blocker: CleanupAction | None,
    overview_comments: dict[int, GithubIssueComment | None],
) -> bool:
    """Apply one planned cleanup, returning whether a partial failure must stop the pass."""

    identity = candidate.pr_identity
    if not isinstance(preflight, PRCleanup):
        if preflight is not None:
            record_action(preflight)
        return False
    pr = preflight.pr
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    if stack_blocker is not None:
        record_action(stack_blocker)
        return False
    overview_comment = overview_comments[identity.pr_number]
    if preflight.close_to is not None:
        trunk_branch = preflight.close_to
        body = t"close {pr_label}"
        if pr.base.ref != trunk_branch:
            body = t"retarget {pr_label} to {ui.bookmark(trunk_branch)}, then close it"
        if not prepared_cleanup.dry_run:
            reason = await close_pr_on_trunk(
                github_client=github_client,
                pr=pr,
                trunk_branch=trunk_branch,
            )
            if reason is not None:
                record_action(CleanupAction(kind="pull request", status="blocked", body=reason))
                return True
        record_action(
            CleanupAction(
                kind="pull request",
                status="planned" if prepared_cleanup.dry_run else "applied",
                body=body,
            )
        )
    return await _apply_tracked_pr_cleanup(
        branch_update=preflight.update,
        overview_comment=overview_comment,
        github_client=github_client,
        pr=pr,
        candidate=candidate,
        change_id=change_id,
        prepared_cleanup=prepared_cleanup,
        record_action=record_action,
        remote_name=remote_name,
    )


def _preflight_tracked_pr_cleanup(
    *,
    initial_observation: RepoFacts,
    change_id: ChangeId,
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int],
    preview_local_removals: frozenset[ChangeId],
) -> CleanupPreflight:
    state = check_tracked_pr(change_id=change_id, observation=initial_observation)
    if isinstance(state, CleanupAction):
        return state
    local_commits = state.local
    pr = state.pr
    if pr.state == "open" and not prepared_cleanup.close_open_prs:
        pr_label = format_pr_label(pr.number, url=pr.html_url)
        action = (
            CleanupAction(
                kind="tracking",
                status="skipped",
                body=t"keep open orphan {pr_label}; to close it, run "
                t"{ui.cmd(f'jj-stack cleanup --pull-request {pr.number} --close')}",
            )
            if not local_commits
            else None
        )
        return action
    update, blocker = plan_pr_cleanup(
        observation=initial_observation,
        preview_detached_dependents=preview_detached_dependents,
        state=state,
    )
    if blocker is not None:
        return blocker
    if (
        pr.state == "merged"
        and change_id not in preview_local_removals
        and any(not commit.immutable for commit in local_commits)
    ):
        pr_label = format_pr_label(pr.number, url=pr.html_url)
        action = CleanupAction(
            kind="tracking",
            status="skipped",
            body=t"keep the saved link for merged {pr_label}: "
            t"{ui.change_id(change_id)} is still in local history; run "
            t"{ui.cmd(f'jj-stack sync {short_change_id(change_id)}')} before cleanup",
        )
        return action
    return PRCleanup(pr=pr, update=update)


async def _apply_tracked_pr_cleanup(
    *,
    branch_update: PRRefUpdate | None,
    overview_comment: GithubIssueComment | None,
    github_client: GithubClient,
    pr: GithubPR,
    candidate: TrackedPR,
    change_id: ChangeId,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
) -> bool:
    """Apply checked cleanup, returning whether a partial failure must stop the pass."""

    mutation_started = not prepared_cleanup.dry_run and (
        branch_update is not None or overview_comment is not None
    )
    apply_remote_branch_cleanup(
        dry_run=prepared_cleanup.dry_run,
        jj_client=prepared_cleanup.context.jj_client,
        record_action=record_action,
        remote_name=remote_name,
        update=branch_update,
    )
    comment_actions, comments_current = await apply_overview_comment_cleanup(
        comment=overview_comment,
        dry_run=prepared_cleanup.dry_run,
        github_client=github_client,
        pr_number=candidate.pr_identity.pr_number,
    )
    for action in comment_actions:
        record_action(action)
    if not comments_current:
        return mutation_started
    action = CleanupAction(
        kind="tracking",
        status="planned" if prepared_cleanup.dry_run else "applied",
        body=t"forget the saved link between {format_pr_label(pr.number, url=pr.html_url)} and "
        t"{ui.change_id(change_id)}",
    )
    if prepared_cleanup.dry_run:
        record_action(action)
    else:
        prepared_cleanup.context.state_store.remove_pr(
            change_id,
        )
        record_action(action)
    return False
