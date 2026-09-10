"""Plan sync for stacks affected by merges across the repo."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from jj_stack.bootstrap import CommandContext
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient
from jj_stack.identifiers import ChangeId, CommitId
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
from jj_stack.stack.repo import observe_repo_paths
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
    remote_name: str,
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
            change_ids=change_ids,
            context=context,
            github_client=github,
            include_remote_targets=False,
            local_commits=observed,
            remote_name=remote_name,
            state=state,
        )
    )
    stacks_task = asyncio.create_task(observe_github_stacks(github=github))
    await wait_for_read_tasks(prs_task, stacks_task)
    pr_observations = prs_task.result()
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


def build_global_convergence_plan(*, facts: GlobalSyncFacts) -> GlobalConvergencePlan:
    state = facts.state
    blocked: list[tuple[ChangeId, TrackedPR, Message]] = []
    finishes: list[OnTrunkChange] = []
    heads: list[ChangeId] = []
    tracked_prs = frozenset(tracked.pr_identity.pr_number for tracked in state.prs.values())
    for change_id, candidate in sorted(state.prs.items()):
        reason, finish, candidate_heads = _classify_global_candidate(
            change_id=change_id,
            candidate=candidate,
            facts=facts,
            tracked_pr_numbers=tracked_prs,
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
    tracked_pr_numbers: frozenset[int],
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
            tracked_prs=tracked_pr_numbers,
        )
    if isinstance(state, PRMissing):
        return state.reason, None, ()
    if ancestry == "unresolved":
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
    tracked_prs: frozenset[int],
) -> tuple[Message | None, OnTrunkChange | None, tuple[ChangeId, ...]]:
    if heads is None:
        return "local history is not a supported stack", None, ()
    if heads:
        return None, None, heads
    if isinstance(state, (PRMissing, PRAmbiguous)):
        return state.reason, None, ()
    if not isinstance(state, Landed):
        return trunk_evidence_reason(state), None, ()
    stack_reason, historical = _detached_stack_blocker(
        candidate=candidate,
        facts=facts,
        tracked_pr_numbers=tracked_prs,
    )
    if stack_reason is not None:
        return stack_reason, None, ()
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


def _detached_stack_blocker(
    *,
    candidate: TrackedPR,
    facts: GlobalSyncFacts,
    tracked_pr_numbers: frozenset[int],
) -> tuple[Message | None, bool]:
    number = candidate.pr_identity.pr_number
    matching = tuple(
        member for stack in facts.stacks for member in stack.prs if member.number == number
    )
    if not matching:
        return None, False
    pr_label = format_pr_label(number, repo=facts.pr_facts.repo)
    if not matching[0].is_historical:
        return t"GitHub still lists {pr_label} among the unmerged PRs in its stack", False
    blocked = any(
        number in stack.pr_numbers
        and not set(stack.active_pr_numbers).isdisjoint(tracked_pr_numbers)
        for stack in facts.stacks
    )
    return (
        (
            t"{pr_label} is in a GitHub stack with unmerged PRs still linked to local changes"
            if blocked
            else None
        ),
        True,
    )
