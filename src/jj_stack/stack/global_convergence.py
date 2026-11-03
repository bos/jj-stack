"""Plan sync for stacks affected by merges across the repo."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace

from jj_stack.bootstrap import CommandContext
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR, TrackingState
from jj_stack.stack.change_state import (
    Closed,
    Landed,
    Merged,
    PRAmbiguous,
    PRIdentityMismatch,
    PRMissing,
    TrackedPRState,
    classify,
    trunk_evidence_reason,
)
from jj_stack.stack.convergence_models import (
    OnTrunkChange,
)
from jj_stack.stack.observation import observe_change_copies
from jj_stack.stack.path import RepoStackPath
from jj_stack.stack.pr_facts import (
    RepoFacts,
    classify_observed_commit_ancestries,
    observe_github_stacks,
    observe_prs,
)
from jj_stack.stack.pr_rewrites import find_head_rewrites
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.trunk import observe_trunk_branch
from jj_stack.stack.trunk_evidence import CommitAncestry
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class GlobalConvergencePlan:
    blocked: tuple[tuple[ChangeId, TrackedPR, Message], ...]
    finishes: tuple[OnTrunkChange, ...]
    sync_change_ids: tuple[ChangeId, ...]


@dataclass(frozen=True, slots=True)
class GlobalSyncFacts:
    """One repo-wide observation for global classification."""

    ancestries: Mapping[CommitId, CommitAncestry]
    local_copies: Mapping[ChangeId, tuple[LocalCommit, ...]]
    paths: tuple[RepoStackPath, ...]
    pr_facts: RepoFacts
    stacks: tuple[GithubStack, ...]
    state: TrackingState


async def observe_global_sync(
    *,
    context: CommandContext,
    github: GithubClient,
    remote: GitRemote,
    trunk_commit_id: CommitId,
) -> GlobalSyncFacts:
    """Observe tracked pull requests from tracking toward affected local paths."""

    state = context.state_store.load()
    change_ids = tuple(sorted(state.prs))
    observed = observe_change_copies(
        jj_client=context.jj_client, state=state, change_ids=change_ids
    )
    local_copies = observed.copies(change_ids, off_trunk=True)
    anchors = tuple(commit.commit_id for commits in local_copies.values() for commit in commits)
    paths = (
        observe_repo_paths(
            jj_client=context.jj_client,
            descendant_of=anchors,
            state=state,
        ).paths
        if anchors
        else ()
    )
    prs_task = asyncio.create_task(
        observe_prs(
            branch_reads="none",
            change_ids=change_ids,
            context=context,
            github_client=github,
            local_commits=observed,
            remote_name=remote.name,
            state=state,
        )
    )
    stacks_task = asyncio.create_task(observe_github_stacks(github=github))
    await wait_for_read_tasks(prs_task, stacks_task)
    pr_observations = await _observe_merged_rewrites(
        context=context,
        github=github,
        observation=prs_task.result(),
        remote=remote,
        trunk_commit_id=trunk_commit_id,
    )
    stacks = stacks_task.result()
    return GlobalSyncFacts(
        ancestries=classify_observed_commit_ancestries(
            context=context,
            observation=pr_observations,
            trunk_commit_id=trunk_commit_id,
        ),
        local_copies=local_copies,
        paths=paths,
        pr_facts=pr_observations,
        stacks=stacks,
        state=state,
    )


async def _observe_merged_rewrites(
    *,
    context: CommandContext,
    github: GithubClient,
    observation: RepoFacts,
    remote: GitRemote,
    trunk_commit_id: CommitId,
) -> RepoFacts:
    """Mark merged PRs whose head is GitHub's rewrite of the submitted commit."""

    if not any(
        item.pr is not None
        and item.pr.state == "merged"
        and item.pr.head.sha != item.tracked.submitted_baseline.commit_id
        for item in observation.prs.values()
    ):
        return observation
    merged = tuple(
        item
        for item in observation.prs.values()
        if item.pr is not None and item.pr.state == "merged"
    )
    trunk_branch, _targets = observe_trunk_branch(
        jj_client=context.jj_client,
        github_repo_state=observation.github_repo,
        remote=remote,
        trunk_commit_id=trunk_commit_id,
    )
    rewrites = await find_head_rewrites(
        context.jj_client, github, remote=remote.name, trunk_branch=trunk_branch, changes=merged
    )
    return replace(
        observation,
        prs={
            change_id: replace(item, rewrite_parent=rewrites[change_id])
            if change_id in rewrites
            else item
            for change_id, item in observation.prs.items()
        },
    )


def build_global_convergence_plan(*, facts: GlobalSyncFacts) -> GlobalConvergencePlan:
    state = facts.state
    blocked: list[tuple[ChangeId, TrackedPR, Message]] = []
    finishes: list[OnTrunkChange] = []
    heads: list[ChangeId] = []
    for change_id, candidate in sorted(state.prs.items()):
        reason, finish, candidate_heads = _classify_global_candidate(
            change_id=change_id,
            candidate=candidate,
            facts=facts,
        )
        heads.extend(candidate_heads)
        if reason is not None:
            blocked.append((change_id, candidate, reason))
        if finish is not None:
            finishes.append(finish)
    return GlobalConvergencePlan(
        blocked=tuple(blocked),
        finishes=tuple(finishes),
        sync_change_ids=tuple(dict.fromkeys(heads)),
    )


def _classify_global_candidate(
    *,
    change_id: ChangeId,
    candidate: TrackedPR,
    facts: GlobalSyncFacts,
) -> tuple[Message | None, OnTrunkChange | None, tuple[ChangeId, ...]]:
    ancestry = facts.ancestries[candidate.submitted_baseline.commit_id]
    state = classify(facts.pr_facts.prs[change_id], ancestries=facts.ancestries)
    heads = _candidate_path_heads(change_id, facts=facts)
    rewritten = isinstance(state, Landed) and state.evidence == "rewritten"
    affected = ancestry == "on_trunk" or rewritten
    if affected:
        return _affected_candidate_plan(
            candidate=candidate,
            facts=facts,
            heads=heads,
            state=state,
        )
    if isinstance(state, PRMissing):
        return state.reason, None, ()
    if ancestry == "unresolved" and not facts.local_copies[change_id]:
        return "the submitted commit is unavailable locally", None, ()
    if isinstance(state, (PRIdentityMismatch, Closed, Merged)):
        return trunk_evidence_reason(state), None, ()
    return None, None, ()


def _affected_candidate_plan(
    *,
    candidate: TrackedPR,
    facts: GlobalSyncFacts,
    heads: tuple[ChangeId, ...] | None,
    state: TrackedPRState,
) -> tuple[Message | None, OnTrunkChange | None, tuple[ChangeId, ...]]:
    if heads is None:
        return "local history is not a supported stack", None, ()
    if heads:
        return None, None, heads
    if isinstance(state, (PRMissing, PRAmbiguous)):
        return state.reason, None, ()
    if not isinstance(state, Landed):
        return trunk_evidence_reason(state), None, ()
    stack_reason, historical, dependents = _detached_stack_members(
        candidate=candidate, facts=facts
    )
    if stack_reason is not None:
        return stack_reason, None, ()
    dependent_heads: list[ChangeId] = []
    for dependent in dependents:
        dependent_path_heads = _candidate_path_heads(dependent, facts=facts)
        if dependent_path_heads is None:
            return (
                "the open PRs in its GitHub stack are linked to local history that is not a "
                "supported stack",
                None,
                (),
            )
        dependent_heads.extend(dependent_path_heads)
    if dependent_heads:
        # Syncing the stack that still holds the open PRs reads this merged PR through its saved
        # link and cleans the link up itself.
        return None, None, tuple(dependent_heads)
    finished = state.evidence == "rewritten" or historical or state.pr.state != "open"
    finish = OnTrunkChange(
        change_id=state.change_id,
        candidate=candidate,
        evidence_kind=state.evidence,
        close_pr=None if finished else state.pr,
        change=None,
    )
    return None, finish, ()


def _candidate_path_heads(
    change_id: ChangeId, *, facts: GlobalSyncFacts
) -> tuple[ChangeId, ...] | None:
    copies = {commit.commit_id for commit in facts.local_copies[change_id]}
    if not copies:
        return ()
    heads = tuple(
        path.stack.head.change_id
        for path in facts.paths
        if any(change.commit_id in copies for change in path.stack.changes)
    )
    return heads or None


def _detached_stack_members(
    *,
    candidate: TrackedPR,
    facts: GlobalSyncFacts,
) -> tuple[Message | None, bool, tuple[ChangeId, ...]]:
    """Read a merged PR's GitHub stack: a blocking reason, whether GitHub lists the PR as merged
    history, and the tracked changes whose PRs are still active in that stack."""

    number = candidate.pr_identity.pr_number
    stacks = tuple(stack for stack in facts.stacks if number in stack.pr_numbers)
    if not stacks:
        return None, False, ()
    pr_label = format_pr_label(number, repo=facts.pr_facts.repo)
    member = next(member for member in stacks[0].prs if member.number == number)
    if not member.is_historical:
        return t"GitHub still lists {pr_label} among the unmerged PRs in its stack", False, ()
    active = {pr_number for stack in stacks for pr_number in stack.active_pr_numbers}
    dependents = tuple(
        change_id
        for change_id, tracked in sorted(facts.state.prs.items())
        if tracked.pr_identity.pr_number in active
    )
    return None, True, dependents
