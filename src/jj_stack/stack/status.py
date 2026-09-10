"""Stack status preparation and GitHub inspection helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ErrorMessage, error_message
from jj_stack.github.client import (
    GithubClient,
    GithubClientError,
)
from jj_stack.github.error_messages import github_action_error_message
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
)
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.change_state import (
    UNOBSERVED,
    ChangeObservation,
    ChangeState,
    ObservationFailed,
    classify,
    live_pr,
    report_incomplete,
)
from jj_stack.stack.preparation import PreparedLocalStack

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StackStatusChange:
    """One local change with its classified pull request state."""

    change: LocalCommit
    tracked: TrackedPR | None
    state: ChangeState

    @property
    def change_id(self) -> ChangeId:
        return self.change.change_id

    @property
    def commit_id(self) -> CommitId:
        return self.change.commit_id

    @property
    def subject(self) -> str:
        return self.change.subject

    @property
    def branch(self) -> str | None:
        return self.tracked.pr_identity.head_ref if self.tracked is not None else None

    @property
    def pr(self) -> GithubPR | None:
        return live_pr(self.state)


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


def observe_status(
    *,
    context: CommandContext,
    prepared: tuple[PreparedLocalStack, ...],
    exclude_branches: frozenset[str] = frozenset(),
) -> dict[str, ChangeObservation] | CliError:
    """Observe the saved PRs of selected stacks in one repository."""

    if not prepared:
        return {}
    target = prepared[0].github_target
    if not isinstance(target, GithubTarget):
        return {}
    observations: dict[str, ChangeObservation] = {}
    for stack in prepared:
        for change in stack.stack.changes:
            observation = _local_observation(stack, change)
            if (branch := observation.branch) is not None and branch not in exclude_branches:
                observations[branch] = observation
    if not observations:
        return {}
    try:
        return asyncio.run(
            lookup_pr_lookups_async(
                context=context, github_repo=target.repo, observations=observations
            )
        )
    except CliError as error:
        logger.debug("status github inspection failed: %s", error_message(error))
        return error


def build_status_result(
    *,
    prepared: PreparedLocalStack,
    pr_lookups: dict[str, ChangeObservation] | CliError,
) -> StatusResult:
    """Classify one local stack using the shared GitHub observation."""

    target = prepared.github_target
    github_repo = target.repo if isinstance(target, GithubTarget) else None
    github_error = target.github_repo_error
    if isinstance(pr_lookups, CliError) and any(
        change.change_id in prepared.state.prs for change in prepared.stack.changes
    ):
        github_error = error_message(pr_lookups)
    lookups = {} if isinstance(pr_lookups, CliError) else pr_lookups
    changes: list[StackStatusChange] = []
    for change in reversed(prepared.stack.changes):
        observation = _local_observation(prepared, change)
        lookup = lookups.get(branch) if (branch := observation.branch) is not None else None
        if lookup is not None:
            observation = replace(
                observation, pr=lookup.pr, open_prs_on_branch=lookup.open_prs_on_branch
            )
        changes.append(
            StackStatusChange(
                change=change, tracked=observation.tracked, state=classify(observation)
            )
        )
    return StatusResult(
        github_error=github_error,
        github_repo=github_repo,
        incomplete=any(report_incomplete(change.state) for change in changes),
        remote=target.remote,
        remote_error=target.remote_error,
        changes=tuple(changes),
        selected_revset=prepared.stack.selected_revset,
    )


def _local_observation(prepared: PreparedLocalStack, change: LocalCommit) -> ChangeObservation:
    tracked = prepared.state.prs.get(change.change_id)
    remote = prepared.github_target.remote
    return ChangeObservation(
        change_id=change.change_id,
        tracked=tracked,
        branch=tracked.pr_identity.head_ref if tracked is not None else None,
        remote_name=remote.name if remote is not None else None,
        local=(change,),
        selected=change,
    )


async def lookup_pr_lookups_async(
    *,
    context: CommandContext,
    github_repo: GithubRepoAddress,
    observations: Mapping[str, ChangeObservation],
) -> dict[str, ChangeObservation]:
    """Look up the saved PR on each branch with a client for this repository."""

    async with context.open_github_client(repo=github_repo) as github_client:
        return await discover_pr_lookups(github_client=github_client, observations=observations)


async def discover_pr_lookups(
    *,
    github_client: GithubClient,
    observations: Mapping[str, ChangeObservation],
) -> dict[str, ChangeObservation]:
    """Fetch the open pull requests on each branch, then each saved PR that is not one of them.

    A branch with no saved pull request yields only the open pull requests GitHub reports for
    it, which is what a first submit needs to know.
    """

    branches = tuple(observations)
    if not branches:
        return {}

    try:
        open_prs_by_branch = await github_client.get_open_prs_by_head_refs(head_refs=branches)
    except GithubClientError as error:
        # Auth failures, missing repos, server errors, and transport failures are repo-level: no
        # per-branch lookup can succeed, so fail the whole inspection rather than reporting
        # per-branch errors.
        status_code = error.status_code
        if status_code is None or status_code in {401, 403, 404} or status_code >= 500:
            raise CliError(
                "",
                hint=t"Run {ui.cmd('jj-stack doctor')} to check GitHub access.",
            ) from error
        lookup_error = github_action_error_message(action="pull request lookup", error=error)
        return {
            branch: replace(
                observations[branch], open_prs_on_branch=ObservationFailed(lookup_error)
            )
            for branch in branches
        }

    saved_open = {
        branch: next(
            (
                pr
                for pr in open_prs_by_branch.get(branch, ())
                if pr.number == tracked.pr_identity.pr_number
            ),
            None,
        )
        for branch, observation in observations.items()
        if (tracked := observation.tracked) is not None
    }
    # The saved PR number is the one reported. Look it up directly when it is not among the
    # open pull requests on its branch.
    remembered = {
        branch: tracked.pr_identity.pr_number
        for branch, observation in observations.items()
        if (tracked := observation.tracked) is not None and saved_open[branch] is None
    }
    remembered_prs: Mapping[int, GithubPR | None | ObservationFailed] = {}
    if remembered:
        try:
            remembered_prs = await github_client.get_prs_by_numbers(
                pr_numbers=tuple(remembered.values()),
            )
        except GithubClientError as error:
            failure = ObservationFailed(
                github_action_error_message(action="saved pull request lookup", error=error)
            )
            remembered_prs = dict.fromkeys(remembered.values(), failure)
    return {
        branch: replace(
            observation,
            pr=(
                remembered_prs.get(number)
                if (number := remembered.get(branch)) is not None
                else saved_open.get(branch, UNOBSERVED)
            ),
            open_prs_on_branch=open_prs_by_branch.get(branch, ()),
        )
        for branch, observation in observations.items()
    }
