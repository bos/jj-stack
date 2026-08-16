"""Review status preparation and GitHub inspection helpers."""

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
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.change_status import (
    classify_review_status_revision,
    submitted_state_disagreement,
)
from jj_stack.review.selected import select_review_path, select_review_path_containing_change
from jj_stack.ui import Message

logger = logging.getLogger(__name__)

HELP = "Check the review status of one or more `jj` stacks"

PullRequestLookupState = Literal["ambiguous", "closed", "error", "missing", "open"]
PullRequestLookupSource = Literal["head", "remembered"]


@dataclass(frozen=True, slots=True)
class PullRequestLookup:
    """Best-effort GitHub pull request lookup for one branch."""

    message: ErrorMessage | None
    pull_request: GithubPullRequest | None
    state: PullRequestLookupState
    review_decision: str | None = None
    review_decision_error: str | None = None
    source: PullRequestLookupSource = "head"


@dataclass(frozen=True, slots=True)
class ReviewStatusRevision:
    """Rendered pull-request and branch state for one local revision."""

    branch: str | None
    change_id: str
    commit_id: str
    local_divergent: bool
    review_identity: ReviewIdentity | None
    submitted_baseline: SubmittedBaseline | None
    pull_request_lookup: PullRequestLookup | None
    remote_target: str | None
    subject: str

    def pull_request(self) -> GithubPullRequest | None:
        lookup = self.pull_request_lookup
        if lookup is None:
            return None
        return lookup.pull_request

    def pull_request_number(self) -> int | None:
        pull_request = self.pull_request()
        if pull_request is None:
            return None
        return pull_request.number


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Status result for one selected local stack."""

    github_error: ErrorMessage | None
    github_repository: GithubRepoAddress | None
    incomplete: bool
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    revisions: tuple[ReviewStatusRevision, ...]
    selected_revset: str
    submitted_state_disagreements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedStatus:
    """Locally prepared status inputs before any GitHub inspection."""

    github_target: GithubTarget | UnresolvedGithubTarget
    prepared: PreparedStack

    @property
    def github_repository(self) -> GithubRepoAddress | None:
        target = self.github_target
        return target.repository if isinstance(target, GithubTarget) else None

    @property
    def github_repository_error(self) -> ErrorMessage | None:
        return self.github_target.github_repository_error

    def github_inspection_count(self) -> int:
        """Return how many selected revisions need live GitHub inspection."""

        if self.github_repository is None:
            return 0
        return sum(
            1
            for prepared_revision in self.prepared.status_revisions
            if _needs_github_inspection(prepared_revision)
        )


@dataclass(frozen=True, slots=True)
class PreparedStack:
    """Prepared local stack inputs shared across inspection-driven commands."""

    client: JjClient
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    remote_targets: dict[str, str]
    stack: LocalStack
    state: ReviewState
    status_revisions: tuple[PreparedRevision, ...]


@dataclass(frozen=True, slots=True)
class PreparedRevision:
    """Local review revision with its optional saved branch and cached state."""

    branch: str | None
    revision: LocalRevision
    review_identity: ReviewIdentity | None
    submitted_baseline: SubmittedBaseline | None


def _required_branch(revision: PreparedRevision) -> str:
    if revision.branch is None:
        raise AssertionError("GitHub inspection requires an exact saved review branch.")
    return revision.branch


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
        selected_path = select_review_path_containing_change(
            change_id=containing_change_id,
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            namespace=context.review_namespace,
            state=state,
        )
    else:
        selected_path = select_review_path(
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            namespace=context.review_namespace,
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
        "status prepared: selected_revset=%s revisions=%d remote=%s",
        prepared.stack.selected_revset,
        len(prepared.status_revisions),
        prepared.remote.name if prepared.remote is not None else "unavailable",
    )
    return PreparedStatus(
        github_target=github_target,
        prepared=prepared,
    )


def stream_status(
    *,
    prepared_status: PreparedStatus,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None = None,
) -> StatusResult:
    """Inspect GitHub state for a prepared stack and optionally stream results out."""

    return asyncio.run(
        stream_status_async(
            on_revision=on_revision,
            prepared_status=prepared_status,
        )
    )


async def stream_status_async(
    *,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None,
    prepared_status: PreparedStatus,
) -> StatusResult:
    prepared = prepared_status.prepared
    selected_revset = prepared.stack.selected_revset
    github_repository = prepared_status.github_repository
    github_repository_error = prepared_status.github_repository_error
    submitted_disagreements = submitted_state_disagreement(
        prepared.state,
        (prepared.stack,),
    )

    if prepared.remote is None:
        display_revisions = tuple(reversed(build_status_revisions_for_prepared_stack(prepared)))
        for revision in display_revisions:
            if on_revision is not None:
                on_revision(revision, False)
        return StatusResult(
            github_error=None,
            github_repository=None,
            incomplete=True,
            remote=None,
            remote_error=prepared.remote_error,
            revisions=display_revisions,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    if github_repository is None:
        logger.debug("status github target unavailable: %s", github_repository_error)
        display_revisions = tuple(reversed(build_status_revisions_for_prepared_stack(prepared)))
        for revision in display_revisions:
            if on_revision is not None:
                on_revision(revision, False)
        return StatusResult(
            github_error=github_repository_error,
            github_repository=None,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            revisions=display_revisions,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    if not prepared.status_revisions:
        return StatusResult(
            github_error=None,
            github_repository=github_repository,
            incomplete=False,
            remote=prepared.remote,
            remote_error=None,
            revisions=(),
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    fallback_revisions = tuple(reversed(build_status_revisions_for_prepared_stack(prepared)))
    prepared_revisions_for_github = tuple(
        prepared_revision
        for prepared_revision in prepared.status_revisions
        if _needs_github_inspection(prepared_revision)
    )
    if not prepared_revisions_for_github:
        return StatusResult(
            github_error=None,
            github_repository=github_repository,
            incomplete=_status_is_incomplete(fallback_revisions),
            remote=prepared.remote,
            remote_error=None,
            revisions=fallback_revisions,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    revisions: list[ReviewStatusRevision] = []
    try:
        async for revision in _iter_status_revisions_with_github(
            github_repository=github_repository,
            prepared=prepared,
            prepared_revisions=prepared_revisions_for_github,
        ):
            revisions.append(revision)
            if on_revision is not None:
                on_revision(revision, True)
    except CliError as error:
        github_error = error_message(error)
        logger.debug("status github inspection failed: %s", github_error)
        streamed_change_ids = {revision.change_id for revision in revisions}
        for revision in fallback_revisions:
            if on_revision is not None and revision.change_id not in streamed_change_ids:
                on_revision(revision, False)
        return StatusResult(
            github_error=github_error,
            github_repository=github_repository,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            revisions=fallback_revisions,
            selected_revset=selected_revset,
            submitted_state_disagreements=submitted_disagreements,
        )

    revisions_by_change_id = {revision.change_id: revision for revision in revisions}
    display_revisions = tuple(
        revisions_by_change_id.get(revision.change_id, revision)
        for revision in fallback_revisions
    )
    return StatusResult(
        github_error=None,
        github_repository=github_repository,
        incomplete=_status_is_incomplete(display_revisions),
        remote=prepared.remote,
        remote_error=None,
        revisions=display_revisions,
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
    state: ReviewState,
) -> PreparedStack:
    """Build prepared status inputs for one already-resolved local stack."""

    jj_client = context.jj_client
    status_revisions = tuple(
        PreparedRevision(
            branch=(identity.head_ref if identity is not None else None),
            revision=revision,
            review_identity=identity,
            submitted_baseline=state.submitted_baselines.get(revision.change_id),
        )
        for revision in stack.revisions
        for identity in (state.review_identities.get(revision.change_id),)
    )
    branches = tuple(
        revision.branch for revision in status_revisions if revision.branch is not None
    )
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
        status_revisions=status_revisions,
    )


def observe_remote_targets_for_status(
    *,
    context: CommandContext,
    excluded_branches: frozenset[str] = frozenset(),
    remote: GitRemote | None,
    stacks: tuple[LocalStack, ...],
    state: ReviewState,
) -> dict[str, str]:
    """Observe the union of exact saved review refs needed for status."""

    if remote is None:
        return {}
    branches = tuple(
        dict.fromkeys(
            identity.head_ref
            for stack in stacks
            for revision in stack.revisions
            for identity in (state.review_identities.get(revision.change_id),)
            if identity is not None and identity.head_ref not in excluded_branches
        )
    )
    if not branches:
        return {}
    return context.jj_client.list_remote_branches(
        remote=remote.name,
        patterns=tuple(f"refs/heads/{branch}" for branch in branches),
    )


def build_status_revisions_for_prepared_stack(
    prepared: PreparedStack,
    *,
    pull_request_lookups: dict[str, PullRequestLookup] | None = None,
) -> tuple[ReviewStatusRevision, ...]:
    return tuple(
        ReviewStatusRevision(
            branch=revision.branch,
            change_id=revision.revision.change_id,
            commit_id=revision.revision.commit_id,
            local_divergent=revision.revision.divergent,
            pull_request_lookup=(
                pull_request_lookups.get(revision.branch)
                if pull_request_lookups is not None and revision.branch is not None
                else None
            ),
            remote_target=(
                prepared.remote_targets.get(revision.branch)
                if revision.branch is not None
                else None
            ),
            review_identity=revision.review_identity,
            submitted_baseline=revision.submitted_baseline,
            subject=revision.revision.subject,
        )
        for revision in prepared.status_revisions
    )


def _needs_github_inspection(prepared_revision: PreparedRevision) -> bool:
    return prepared_revision.review_identity is not None


def _status_is_incomplete(revisions: tuple[ReviewStatusRevision, ...]) -> bool:
    return any(
        classify_review_status_revision(revision).makes_report_incomplete
        for revision in revisions
    )


async def _iter_status_revisions_with_github(
    *,
    github_repository: GithubRepoAddress,
    prepared: PreparedStack,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> AsyncIterator[ReviewStatusRevision]:
    ordered_prepared_revisions = tuple(reversed(prepared_revisions))
    async with build_github_client(repository=github_repository) as github_client:
        pull_request_lookups = await _resolve_pull_request_lookups(
            github_client=github_client,
            on_progress=None,
            prepared_revisions=ordered_prepared_revisions,
        )
        for prepared_revision in ordered_prepared_revisions:
            branch = _required_branch(prepared_revision)
            pull_request_lookup = pull_request_lookups[branch]
            logger.debug(
                "status revision inspected: change_id=%s branch=%s pr_state=%s",
                short_change_id(prepared_revision.revision.change_id),
                branch,
                pull_request_lookup.state,
            )
            yield ReviewStatusRevision(
                branch=branch,
                change_id=prepared_revision.revision.change_id,
                commit_id=prepared_revision.revision.commit_id,
                local_divergent=prepared_revision.revision.divergent,
                pull_request_lookup=pull_request_lookup,
                remote_target=prepared.remote_targets.get(branch),
                review_identity=prepared_revision.review_identity,
                submitted_baseline=prepared_revision.submitted_baseline,
                subject=prepared_revision.revision.subject,
            )


def lookup_pull_request_lookups(
    *,
    github_repository: GithubRepoAddress,
    on_progress: Callable[[int], None] | None = None,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    """Return batched pull-request lookups keyed by saved branch."""

    return asyncio.run(
        lookup_pull_request_lookups_async(
            github_repository=github_repository,
            on_progress=on_progress,
            prepared_revisions=prepared_revisions,
        )
    )


async def lookup_pull_request_lookups_async(
    *,
    github_repository: GithubRepoAddress,
    on_progress: Callable[[int], None] | None = None,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    """Return batched pull-request lookups keyed by saved branch."""

    async with build_github_client(repository=github_repository) as github_client:
        return await _resolve_pull_request_lookups(
            github_client=github_client,
            on_progress=on_progress,
            prepared_revisions=prepared_revisions,
        )


async def _resolve_pull_request_lookups(
    *,
    github_client: GithubClient,
    on_progress: Callable[[int], None] | None,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    pull_request_lookups = await _discover_pull_request_lookups(
        github_client=github_client,
        prepared_revisions=prepared_revisions,
    )
    if on_progress is not None and pull_request_lookups:
        on_progress(len(pull_request_lookups))
    return pull_request_lookups


async def _discover_pull_request_lookups(
    *,
    github_client: GithubClient,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    prepared_revisions_by_branch = {
        _required_branch(prepared_revision): prepared_revision
        for prepared_revision in prepared_revisions
    }
    branches = tuple(prepared_revisions_by_branch)
    if not branches:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            head_refs=branches,
        )
    except GithubClientError as error:
        # Auth failures, missing repositories, server errors, and transport
        # failures are repository-level: no per-branch lookup can succeed, so
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
            branch: PullRequestLookup(
                message=lookup_error,
                pull_request=None,
                state="error",
            )
            for branch in branches
        }

    lookups = {
        branch: _pull_request_lookup_from_discovered(
            head_label=t"{github_client.repository.owner}:{ui.bookmark(branch)}",
            pull_requests=discovered_pull_requests.get(branch, ()),
        )
        for branch in branches
    }
    remembered_numbers = tuple(
        prepared_revision.review_identity.pr_number
        for branch, prepared_revision in prepared_revisions_by_branch.items()
        if lookups[branch].state == "missing" and prepared_revision.review_identity is not None
    )
    if not remembered_numbers:
        return lookups

    try:
        remembered_pull_requests = await github_client.get_pull_requests_by_numbers(
            pull_numbers=remembered_numbers,
        )
    except GithubClientError as error:
        lookup_error = summarize_github_lookup_error(
            action="remembered pull request lookup",
            error=error,
        )
        failed_lookups: dict[str, PullRequestLookup] = {}
        for branch, lookup in lookups.items():
            review_identity = prepared_revisions_by_branch[branch].review_identity
            if lookup.state == "missing" and review_identity is not None:
                failed_lookups[branch] = PullRequestLookup(
                    message=lookup_error,
                    pull_request=None,
                    state="error",
                )
            else:
                failed_lookups[branch] = lookup
        return failed_lookups

    for branch, lookup in tuple(lookups.items()):
        if lookup.state != "missing":
            continue
        review_identity = prepared_revisions_by_branch[branch].review_identity
        if review_identity is None:
            continue
        remembered_pull_request = remembered_pull_requests.get(review_identity.pr_number)
        if remembered_pull_request is None:
            continue
        lookups[branch] = _pull_request_lookup_from_remembered(
            branch=branch,
            pull_request=remembered_pull_request,
        )
    return lookups


def _pull_request_lookup_from_discovered(
    *,
    head_label: Message,
    pull_requests: tuple[GithubPullRequest, ...],
) -> PullRequestLookup:
    if not pull_requests:
        return PullRequestLookup(
            message=None,
            pull_request=None,
            state="missing",
        )
    if len(pull_requests) > 1:
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        return PullRequestLookup(
            message=(
                t"GitHub reports multiple pull requests for head branch {head_label}: {numbers}."
            ),
            pull_request=None,
            state="ambiguous",
        )

    pull_request = pull_requests[0]
    effective_pull_request = pull_request.normalize_state()
    message = None
    if effective_pull_request.state != "open":
        message = (
            t"GitHub reports pull request #{effective_pull_request.number} "
            t"for head branch {head_label} in state {effective_pull_request.state}."
        )
    return _single_pull_request_lookup(
        message=message,
        pull_request=effective_pull_request,
    )


def _pull_request_lookup_from_remembered(
    *,
    branch: str,
    pull_request: GithubPullRequest,
) -> PullRequestLookup:
    effective_pull_request = pull_request.normalize_state()
    message: ErrorMessage | None = None
    if effective_pull_request.head.ref != branch:
        message = (
            t"Remembered PR #{effective_pull_request.number} now uses head branch "
            t"{ui.bookmark(effective_pull_request.head.ref)}, not "
            t"{ui.bookmark(branch)}."
        )
    return _single_pull_request_lookup(
        message=message,
        pull_request=effective_pull_request,
        source="remembered",
    )


def _single_pull_request_lookup(
    *,
    message: ErrorMessage | None,
    pull_request: GithubPullRequest,
    source: PullRequestLookupSource = "head",
) -> PullRequestLookup:
    open_ = pull_request.state == "open"
    return PullRequestLookup(
        message=message,
        pull_request=pull_request,
        review_decision=(
            pull_request.review_decision if open_ and not pull_request.is_draft else None
        ),
        source=source,
        state="open" if open_ else "closed",
    )
