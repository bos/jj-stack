"""Read PR and branch state for command precondition checks."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

import jj_stack.github.resolution as github_resolution
from jj_stack.bootstrap import CommandContext
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_availability import github_stacks_unavailable_error
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR, GithubRepo, GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import (
    PRIdentity,
)
from jj_stack.stack.change_state import UNOBSERVED, TrackedPRObservation
from jj_stack.stack.observation import observe_change_copies
from jj_stack.stack.trunk_evidence import CommitAncestry


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """PR and branch observations used to check command preconditions."""

    configured_repo: github_resolution.GithubRepoAddress | None
    github_repo: GithubRepo
    # Contains an entry, possibly empty, for each branch whose dependents were observed.
    prs_by_base: Mapping[str, tuple[GithubPR, ...]]
    remote: GitRemote | None
    repo: github_resolution.GithubRepoAddress
    prs: Mapping[str, TrackedPRObservation]


def duplicate_pr_claim_change_ids(
    identities: Mapping[str, PRIdentity],
) -> frozenset[str]:
    """Return every change participating in a duplicate PR or head claim."""

    values = identities.values()
    pr_claims = Counter(item.pr_number for item in values)
    head_claims = Counter(item.head_ref for item in values)
    return frozenset(
        change_id
        for change_id, item in identities.items()
        if pr_claims[item.pr_number] > 1 or head_claims[item.head_ref] > 1
    )


async def observe_prs(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
    github_client: GithubClient,
    remote_name: str,
    include_dependents: bool = False,
    include_open_head_prs: bool = False,
    include_remote_targets: bool = True,
    github_repo_snapshot: GithubRepo | None = None,
    local_commits_snapshot: Mapping[str, tuple[LocalCommit, ...]] | None = None,
) -> RepoFacts:
    """Read PR state, optionally skipping branch target lookups."""

    remotes = context.jj_client.list_git_remotes()
    remote = next((item for item in remotes if item.name == remote_name), None)
    state = context.state_store.load()
    tracked_prs = {
        change_id: tracked
        for change_id in dict.fromkeys(change_ids)
        if (tracked := state.prs.get(change_id)) is not None
    }
    repo = github_client.repo
    known_identities = tuple(tracked.pr_identity for tracked in tracked_prs.values())
    head_refs = tuple(dict.fromkeys(identity.head_ref for identity in known_identities))
    pr_numbers = tuple(dict.fromkeys(identity.pr_number for identity in known_identities))
    if local_commits_snapshot is None:

        def observe_local_commits() -> dict[str, tuple[LocalCommit, ...]]:
            return observe_change_copies(
                jj_client=context.jj_client, state=state, change_ids=tuple(tracked_prs)
            ).copies(tuple(tracked_prs))

        local_task = asyncio.create_task(asyncio.to_thread(observe_local_commits))
    else:
        local_task = asyncio.create_task(asyncio.sleep(0, result=local_commits_snapshot))
    if include_open_head_prs:
        open_heads_request = github_client.get_open_prs_by_head_refs(head_refs=head_refs)
    else:
        open_heads_request = asyncio.sleep(0, result={})
    if include_remote_targets and remote is not None and head_refs:
        remote_targets_request = github_client.get_branch_targets(branches=head_refs)
    else:
        remote_targets_request = asyncio.sleep(0, result={})
    numbered_task = asyncio.create_task(github_client.get_prs_by_numbers(pr_numbers=pr_numbers))
    open_heads_task = asyncio.create_task(open_heads_request)
    by_base_task: asyncio.Task[dict[str, tuple[GithubPR, ...]]]
    if include_dependents:
        by_base_task = asyncio.create_task(
            github_client.get_prs_by_base_refs(base_refs=head_refs)
        )
    else:
        by_base_task = asyncio.create_task(asyncio.sleep(0, result={}))
    repo_task = asyncio.create_task(
        github_client.get_repo()
        if github_repo_snapshot is None
        else asyncio.sleep(0, result=github_repo_snapshot)
    )
    remote_targets_task = asyncio.create_task(remote_targets_request)
    try:
        await wait_for_read_tasks(
            numbered_task, open_heads_task, by_base_task, repo_task, remote_targets_task
        )
        local_commits = await asyncio.shield(local_task)
    except BaseException:
        # Cancelling to_thread cannot stop the worker or its jj subprocess. Join it before
        # the caller closes its resources and releases the repo operation lock.
        await asyncio.gather(asyncio.shield(local_task), return_exceptions=True)
        raise
    numbered = numbered_task.result()
    by_head = open_heads_task.result()
    by_base = by_base_task.result()
    github_repo = repo_task.result()
    remote_targets = remote_targets_task.result()
    prs = {
        change_id: TrackedPRObservation(
            change_id=change_id,
            branch=identity.head_ref,
            remote_name=remote.name if remote is not None else None,
            tracked=tracked,
            open_prs_on_branch=(
                by_head.get(identity.head_ref, ()) if include_open_head_prs else UNOBSERVED
            ),
            local=matches,
            pr=numbered.get(identity.pr_number),
            remote_target=(
                remote_targets.get(identity.head_ref)
                if include_remote_targets and remote is not None
                else UNOBSERVED
            ),
        )
        for change_id, tracked in tracked_prs.items()
        for identity in (tracked.pr_identity,)
        for matches in (local_commits.get(change_id, ()),)
    }

    return RepoFacts(
        configured_repo=github_resolution.parse_github_repo(remote) if remote else None,
        github_repo=github_repo,
        prs_by_base=by_base,
        remote=remote,
        repo=repo,
        prs=prs,
    )


def classify_commit_ancestries(
    *,
    commit_ids: tuple[str | None, ...],
    context: CommandContext,
    trunk_commit_id: str,
) -> dict[str, CommitAncestry]:
    """Classify commits in one scan while keeping unavailable commits distinct."""

    present_commit_ids = tuple(commit_id for commit_id in commit_ids if commit_id is not None)
    memberships = context.jj_client.query_present_commit_ancestor_membership(
        present_commit_ids,
        descendant_commit_id=trunk_commit_id,
    )
    states: dict[bool, CommitAncestry] = {True: "on_trunk", False: "not_on_trunk"}
    return {
        commit_id: states[memberships[commit_id]] if commit_id in memberships else "unresolved"
        for commit_id in dict.fromkeys(present_commit_ids)
    }


def classify_observed_commit_ancestries(
    *,
    context: CommandContext,
    observation: RepoFacts,
    trunk_commit_id: str,
) -> dict[str, CommitAncestry]:
    return classify_commit_ancestries(
        commit_ids=tuple(
            commit_id
            for item in observation.prs.values()
            for commit_id in (
                item.tracked.submitted_baseline.commit_id,
                item.pr.merge_commit_sha if item.pr is not None else None,
            )
        ),
        context=context,
        trunk_commit_id=trunk_commit_id,
    )


async def observe_github_stacks(*, github: GithubClient) -> tuple[GithubStack, ...]:
    """List the repo's GitHub stacks, explaining a repo that cannot use the Stacks API."""

    try:
        return await github.list_stacks()
    except GithubClientError as error:
        unavailable = github_stacks_unavailable_error(error=error, repo=github.repo.full_name)
        if unavailable is not None:
            raise unavailable from None
        raise CliError(
            "Could not inspect GitHub stack membership.",
            hint="Resolve the GitHub error above, then rerun the command.",
        ) from error
