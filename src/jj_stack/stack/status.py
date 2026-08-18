"""Stack status preparation and GitHub inspection helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ErrorMessage, error_message
from jj_stack.github.client import (
    GithubClient,
    GithubClientError,
    build_github_client,
)
from jj_stack.github.error_messages import (
    summarize_github_lookup_error,
)
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.identifiers import short_change_id
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.stack.change_status import (
    classify_stack_status_change,
    submitted_state_disagreement,
)
from jj_stack.stack.selected import select_stack_path, select_stack_path_containing_change
from jj_stack.ui import Message

logger = logging.getLogger(__name__)

HELP = "Check one or more `jj` stacks and their pull requests"

PRLookupState = Literal["ambiguous", "closed", "error", "missing", "open"]
PRLookupSource = Literal["head", "remembered"]


@dataclass(frozen=True, slots=True)
class PRLookup:
    """Best-effort GitHub pull request lookup for one branch."""

    message: ErrorMessage | None
    pr: GithubPR | None
    state: PRLookupState
    review_decision: str | None = None
    review_decision_error: str | None = None
    source: PRLookupSource = "head"


@dataclass(frozen=True, slots=True)
class StackStatusChange:
    """Rendered pull-request and branch state for one local change."""

    branch: str | None
    change_id: str
    commit_id: str
    local_divergent: bool
    pr_identity: PRIdentity | None
    submitted_baseline: SubmittedBaseline | None
    pr_lookup: PRLookup | None
    remote_target: str | None
    subject: str

    def pr(self) -> GithubPR | None:
        lookup = self.pr_lookup
        if lookup is None:
            return None
        return lookup.pr

    def pr_number(self) -> int | None:
        pr = self.pr()
        if pr is None:
            return None
        return pr.number


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Status result for one selected local stack."""

    github_error: ErrorMessage | None
    github_repo: GithubRepoAddress | None
    incomplete: bool
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    changes: tuple[StackStatusChange, ...]
    selected_revset: str
    submitted_state_disagreements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedStatus:
    """Locally prepared status inputs before any GitHub inspection."""

    github_target: GithubTarget | UnresolvedGithubTarget
    prepared: PreparedStack

    @property
    def github_repo(self) -> GithubRepoAddress | None:
        target = self.github_target
        return target.repo if isinstance(target, GithubTarget) else None

    @property
    def github_repo_error(self) -> ErrorMessage | None:
        return self.github_target.github_repo_error

    def github_inspection_count(self) -> int:
        """Return how many selected changes need live GitHub inspection."""

        if self.github_repo is None:
            return 0
        return sum(
            1
            for prepared_change in self.prepared.status_changes
            if _needs_github_inspection(prepared_change)
        )


@dataclass(frozen=True, slots=True)
class PreparedStack:
    """Prepared local stack inputs shared across inspection-driven commands."""

    client: JjClient
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    remote_targets: dict[str, str]
    stack: LocalStack
    state: TrackingState
    status_changes: tuple[PreparedChange, ...]


@dataclass(frozen=True, slots=True)
class PreparedChange:
    """Local stack change with its optional saved PR branch and cached state."""

    branch: str | None
    change: LocalCommit
    pr_identity: PRIdentity | None
    submitted_baseline: SubmittedBaseline | None


def _required_branch(change: PreparedChange) -> str:
    if change.branch is None:
        raise AssertionError("GitHub inspection requires an exact saved PR branch.")
    return change.branch


def status_preparation_cli_error(error: UnsupportedStackError) -> CliError:
    """Translate stack-shape preparation failures into a user-facing CLI error."""

    if error.reason == "trunk_resolved_to_root":
        return CliError(
            "No trunk bookmark is configured for this repo.",
            hint=error.hint,
        )
    return CliError(t"Local history does not form a linear stack. {error}")


def prepare_status(
    *,
    context: CommandContext,
    fetch_remote_state: bool = False,
    observe_remote_targets: bool = True,
    revset: str | None,
    containing_change_id: str | None = None,
    inspection_mode: bool = False,
) -> PreparedStatus:
    """Resolve local status inputs before any GitHub network inspection."""

    jj_client = context.jj_client
    state_store = context.state_store
    state = state_store.load()
    github_target = resolve_github_target(jj_client.list_git_remotes())
    if fetch_remote_state and github_target.remote is not None:
        jj_client.fetch_remote(remote=github_target.remote.name)

    if containing_change_id is not None:
        selected_path = select_stack_path_containing_change(
            change_id=containing_change_id,
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            state=state,
        )
    else:
        selected_path = select_stack_path(
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            revset=revset,
            state=state,
        )
    prepared = prepare_stack_for_status(
        context=context,
        observed_remote_targets=None if observe_remote_targets else {},
        remote=github_target.remote,
        remote_error=github_target.remote_error,
        stack=selected_path.stack,
        state=state,
    )
    logger.debug(
        "status prepared: selected_revset=%s changes=%d remote=%s",
        prepared.stack.selected_revset,
        len(prepared.status_changes),
        prepared.remote.name if prepared.remote is not None else "unavailable",
    )
    return PreparedStatus(
        github_target=github_target,
        prepared=prepared,
    )


def stream_status(
    *,
    prepared_status: PreparedStatus,
    on_change: Callable[[StackStatusChange, bool], None] | None = None,
) -> StatusResult:
    """Inspect GitHub state for a prepared stack and optionally stream results out."""

    return asyncio.run(
        stream_status_async(
            on_change=on_change,
            prepared_status=prepared_status,
        )
    )


async def stream_status_async(
    *,
    on_change: Callable[[StackStatusChange, bool], None] | None,
    prepared_status: PreparedStatus,
) -> StatusResult:
    prepared = prepared_status.prepared
    selected_revset = prepared.stack.selected_revset
    github_repo = prepared_status.github_repo
    github_repo_error = prepared_status.github_repo_error
    submitted_disagreements = submitted_state_disagreement(
        prepared.state,
        (prepared.stack,),
    )

    if prepared.remote is None:
        display_changes = tuple(reversed(build_status_changes_for_prepared_stack(prepared)))
        for change in display_changes:
            if on_change is not None:
                on_change(change, False)
        return StatusResult(
            github_error=None,
            github_repo=None,
            incomplete=True,
            remote=None,
            remote_error=prepared.remote_error,
            changes=display_changes,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    if github_repo is None:
        logger.debug("status github target unavailable: %s", github_repo_error)
        display_changes = tuple(reversed(build_status_changes_for_prepared_stack(prepared)))
        for change in display_changes:
            if on_change is not None:
                on_change(change, False)
        return StatusResult(
            github_error=github_repo_error,
            github_repo=None,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            changes=display_changes,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    if not prepared.status_changes:
        return StatusResult(
            github_error=None,
            github_repo=github_repo,
            incomplete=False,
            remote=prepared.remote,
            remote_error=None,
            changes=(),
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    fallback_changes = tuple(reversed(build_status_changes_for_prepared_stack(prepared)))
    prepared_changes_for_github = tuple(
        prepared_change
        for prepared_change in prepared.status_changes
        if _needs_github_inspection(prepared_change)
    )
    if not prepared_changes_for_github:
        return StatusResult(
            github_error=None,
            github_repo=github_repo,
            incomplete=_status_is_incomplete(fallback_changes),
            remote=prepared.remote,
            remote_error=None,
            changes=fallback_changes,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    changes: list[StackStatusChange] = []
    try:
        async for change in _iter_status_changes_with_github(
            github_repo=github_repo,
            prepared=prepared,
            prepared_changes=prepared_changes_for_github,
        ):
            changes.append(change)
            if on_change is not None:
                on_change(change, True)
    except CliError as error:
        github_error = error_message(error)
        logger.debug("status github inspection failed: %s", github_error)
        streamed_change_ids = {change.change_id for change in changes}
        for change in fallback_changes:
            if on_change is not None and change.change_id not in streamed_change_ids:
                on_change(change, False)
        return StatusResult(
            github_error=github_error,
            github_repo=github_repo,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            changes=fallback_changes,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    changes_by_change_id = {change.change_id: change for change in changes}
    display_changes = tuple(
        changes_by_change_id.get(change.change_id, change) for change in fallback_changes
    )
    return StatusResult(
        github_error=None,
        github_repo=github_repo,
        incomplete=_status_is_incomplete(display_changes),
        remote=prepared.remote,
        remote_error=None,
        changes=display_changes,
        selected_revset=selected_revset,
        submitted_state_disagreements=submitted_disagreements,
    )


def prepare_stack_for_status(
    *,
    context: CommandContext,
    observed_remote_targets: dict[str, str] | None = None,
    remote: GitRemote | None,
    remote_error: ErrorMessage | None,
    stack: LocalStack,
    state: TrackingState,
) -> PreparedStack:
    """Build prepared status inputs for one already-resolved local stack."""

    jj_client = context.jj_client
    status_changes = tuple(
        PreparedChange(
            branch=(identity.head_ref if identity is not None else None),
            change=change,
            pr_identity=identity,
            submitted_baseline=state.submitted_baselines.get(change.change_id),
        )
        for change in stack.changes
        for identity in (state.pr_identities.get(change.change_id),)
    )
    branches = tuple(change.branch for change in status_changes if change.branch is not None)
    if observed_remote_targets is None:
        observed_remote_targets = observe_remote_targets_for_status(
            context=context,
            remote=remote,
            stacks=(stack,),
            state=state,
        )
    remote_targets = {
        branch: observed_remote_targets[branch]
        for branch in branches
        if branch in observed_remote_targets
    }
    return PreparedStack(
        client=jj_client,
        remote=remote,
        remote_error=remote_error,
        remote_targets=remote_targets,
        stack=stack,
        state=state,
        status_changes=status_changes,
    )


def observe_remote_targets_for_status(
    *,
    context: CommandContext,
    excluded_branches: frozenset[str] = frozenset(),
    remote: GitRemote | None,
    stacks: tuple[LocalStack, ...],
    state: TrackingState,
) -> dict[str, str]:
    """Observe the union of exact saved PR branch refs needed for status."""

    if remote is None:
        return {}
    branches = tuple(
        dict.fromkeys(
            identity.head_ref
            for stack in stacks
            for change in stack.changes
            for identity in (state.pr_identities.get(change.change_id),)
            if identity is not None and identity.head_ref not in excluded_branches
        )
    )
    if not branches:
        return {}
    return context.jj_client.list_remote_branches(
        remote=remote.name,
        patterns=tuple(f"refs/heads/{branch}" for branch in branches),
    )


def build_status_changes_for_prepared_stack(
    prepared: PreparedStack,
    *,
    pr_lookups: dict[str, PRLookup] | None = None,
) -> tuple[StackStatusChange, ...]:
    return tuple(
        StackStatusChange(
            branch=change.branch,
            change_id=change.change.change_id,
            commit_id=change.change.commit_id,
            local_divergent=change.change.divergent,
            pr_lookup=(
                pr_lookups.get(change.branch)
                if pr_lookups is not None and change.branch is not None
                else None
            ),
            remote_target=(
                prepared.remote_targets.get(change.branch) if change.branch is not None else None
            ),
            pr_identity=change.pr_identity,
            submitted_baseline=change.submitted_baseline,
            subject=change.change.subject,
        )
        for change in prepared.status_changes
    )


def _needs_github_inspection(prepared_change: PreparedChange) -> bool:
    return prepared_change.pr_identity is not None


def _status_is_incomplete(changes: tuple[StackStatusChange, ...]) -> bool:
    return any(classify_stack_status_change(change).makes_report_incomplete for change in changes)


async def _iter_status_changes_with_github(
    *,
    github_repo: GithubRepoAddress,
    prepared: PreparedStack,
    prepared_changes: tuple[PreparedChange, ...],
) -> AsyncIterator[StackStatusChange]:
    ordered_prepared_changes = tuple(reversed(prepared_changes))
    async with build_github_client(repo=github_repo) as github_client:
        pr_lookups = await _resolve_pr_lookups(
            github_client=github_client,
            on_progress=None,
            prepared_changes=ordered_prepared_changes,
        )
        for prepared_change in ordered_prepared_changes:
            branch = _required_branch(prepared_change)
            pr_lookup = pr_lookups[branch]
            logger.debug(
                "status change inspected: change_id=%s branch=%s pr_state=%s",
                short_change_id(prepared_change.change.change_id),
                branch,
                pr_lookup.state,
            )
            yield StackStatusChange(
                branch=branch,
                change_id=prepared_change.change.change_id,
                commit_id=prepared_change.change.commit_id,
                local_divergent=prepared_change.change.divergent,
                pr_lookup=pr_lookup,
                remote_target=prepared.remote_targets.get(branch),
                pr_identity=prepared_change.pr_identity,
                submitted_baseline=prepared_change.submitted_baseline,
                subject=prepared_change.change.subject,
            )


def lookup_pr_lookups(
    *,
    github_repo: GithubRepoAddress,
    on_progress: Callable[[int], None] | None = None,
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, PRLookup]:
    """Return batched pull-request lookups keyed by saved branch."""

    return asyncio.run(
        lookup_pr_lookups_async(
            github_repo=github_repo,
            on_progress=on_progress,
            prepared_changes=prepared_changes,
        )
    )


async def lookup_pr_lookups_async(
    *,
    github_repo: GithubRepoAddress,
    on_progress: Callable[[int], None] | None = None,
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, PRLookup]:
    """Return batched pull-request lookups keyed by saved branch."""

    async with build_github_client(repo=github_repo) as github_client:
        return await _resolve_pr_lookups(
            github_client=github_client,
            on_progress=on_progress,
            prepared_changes=prepared_changes,
        )


async def _resolve_pr_lookups(
    *,
    github_client: GithubClient,
    on_progress: Callable[[int], None] | None,
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, PRLookup]:
    pr_lookups = await _discover_pr_lookups(
        github_client=github_client,
        prepared_changes=prepared_changes,
    )
    if on_progress is not None and pr_lookups:
        on_progress(len(pr_lookups))
    return pr_lookups


async def _discover_pr_lookups(
    *,
    github_client: GithubClient,
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, PRLookup]:
    prepared_changes_by_branch = {
        _required_branch(prepared_change): prepared_change for prepared_change in prepared_changes
    }
    branches = tuple(prepared_changes_by_branch)
    if not branches:
        return {}

    try:
        discovered_prs = await github_client.get_open_prs_by_head_refs(
            head_refs=branches,
        )
    except GithubClientError as error:
        # Auth failures, missing repos, server errors, and transport
        # failures are repo-level: no per-branch lookup can succeed, so
        # fail the whole inspection rather than reporting per-branch errors.
        status_code = error.status_code
        if status_code is None or status_code in {401, 403, 404} or status_code >= 500:
            raise CliError(
                summarize_github_lookup_error(action="pull request lookup", error=error),
                hint=t"Run {ui.cmd('jj-stack doctor')} to check GitHub access.",
            ) from error
        lookup_error = summarize_github_lookup_error(
            action="pull request lookup",
            error=error,
        )
        return {
            branch: PRLookup(
                message=lookup_error,
                pr=None,
                state="error",
            )
            for branch in branches
        }

    lookups = {
        branch: _pr_lookup_from_discovered(
            head_label=t"{github_client.repo.owner}:{ui.bookmark(branch)}",
            prs=discovered_prs.get(branch, ()),
        )
        for branch in branches
    }
    remembered_numbers = tuple(
        prepared_change.pr_identity.pr_number
        for branch, prepared_change in prepared_changes_by_branch.items()
        if lookups[branch].state == "missing" and prepared_change.pr_identity is not None
    )
    if not remembered_numbers:
        return lookups

    try:
        remembered_prs = await github_client.get_prs_by_numbers(
            pr_numbers=remembered_numbers,
        )
    except GithubClientError as error:
        lookup_error = summarize_github_lookup_error(
            action="remembered pull request lookup",
            error=error,
        )
        failed_lookups: dict[str, PRLookup] = {}
        for branch, lookup in lookups.items():
            pr_identity = prepared_changes_by_branch[branch].pr_identity
            if lookup.state == "missing" and pr_identity is not None:
                failed_lookups[branch] = PRLookup(
                    message=lookup_error,
                    pr=None,
                    state="error",
                )
            else:
                failed_lookups[branch] = lookup
        return failed_lookups

    for branch, lookup in tuple(lookups.items()):
        if lookup.state != "missing":
            continue
        pr_identity = prepared_changes_by_branch[branch].pr_identity
        if pr_identity is None:
            continue
        remembered_pr = remembered_prs.get(pr_identity.pr_number)
        if remembered_pr is None:
            continue
        lookups[branch] = _pr_lookup_from_remembered(
            branch=branch,
            pr=remembered_pr,
        )
    return lookups


def _pr_lookup_from_discovered(
    *,
    head_label: Message,
    prs: tuple[GithubPR, ...],
) -> PRLookup:
    if not prs:
        return PRLookup(
            message=None,
            pr=None,
            state="missing",
        )
    if len(prs) > 1:
        numbers = ", ".join(str(pr.number) for pr in prs)
        return PRLookup(
            message=(
                t"GitHub reports multiple pull requests for head branch {head_label}: {numbers}."
            ),
            pr=None,
            state="ambiguous",
        )

    pr = prs[0]
    effective_pr = pr.normalize_state()
    message = None
    if effective_pr.state != "open":
        message = (
            t"GitHub reports pull request #{effective_pr.number} "
            t"for head branch {head_label} in state {effective_pr.state}."
        )
    return _single_pr_lookup(
        message=message,
        pr=effective_pr,
    )


def _pr_lookup_from_remembered(
    *,
    branch: str,
    pr: GithubPR,
) -> PRLookup:
    effective_pr = pr.normalize_state()
    message: ErrorMessage | None = None
    if effective_pr.head.ref != branch:
        message = (
            t"Remembered PR #{effective_pr.number} now uses head branch "
            t"{ui.bookmark(effective_pr.head.ref)}, not "
            t"{ui.bookmark(branch)}."
        )
    return _single_pr_lookup(
        message=message,
        pr=effective_pr,
        source="remembered",
    )


def _single_pr_lookup(
    *,
    message: ErrorMessage | None,
    pr: GithubPR,
    source: PRLookupSource = "head",
) -> PRLookup:
    open_ = pr.state == "open"
    return PRLookup(
        message=message,
        pr=pr,
        review_decision=(pr.review_decision if open_ and not pr.is_draft else None),
        source=source,
        state="open" if open_ else "closed",
    )
