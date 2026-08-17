"""Observe and classify repository-wide convergence without mutating it."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import jj_stack.github.resolution as github_resolution
from jj_stack.bootstrap import CommandContext
from jj_stack.github.client import GithubClient
from jj_stack.models.github import GithubPullRequest, GithubStack
from jj_stack.models.review_state import ReviewState, TrackedReview
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import prepare_visible_review_snapshots
from jj_stack.review.convergence_models import (
    FinishReview,
    ReviewFinishPlan,
    SkipReviewFinish,
)
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_github_stacks,
    observe_reviews,
)
from jj_stack.review.path import RepositoryReviewPath
from jj_stack.review.repository import observe_repository_paths
from jj_stack.review.trunk_evidence import (
    CommitAncestry,
    TrunkEvidenceKind,
    classify_commit_ancestries,
    classify_proven_kind,
)
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class GlobalConvergencePlan:
    blocked: tuple[tuple[TrackedReview, Message], ...]
    finishes: tuple[ReviewFinishPlan, ...]
    sync_change_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GlobalSyncFacts:
    """One repository-wide observation for global classification."""

    ancestries: Mapping[str, CommitAncestry]
    local_copies: Mapping[str, tuple[LocalRevision, ...]]
    paths: tuple[RepositoryReviewPath, ...]
    reviews: RepositoryObservation
    stacks: tuple[GithubStack, ...]


async def observe_global_sync(
    *,
    context: CommandContext,
    github: GithubClient,
    remote_name: str,
    trunk_commit_id: str,
) -> GlobalSyncFacts:
    """Observe tracked reviews from tracking toward affected local paths."""

    state = context.state_store.load()
    candidates = state.tracked_reviews()
    change_ids = tuple(candidate.change_id for candidate in candidates)
    prepare_visible_review_snapshots(
        jj_client=context.jj_client,
        state=state,
    )
    all_copies, local_copies = context.jj_client.query_revisions_by_change_ids_with_off_trunk(
        change_ids
    )
    anchors = tuple(
        revision.commit_id for revisions in local_copies.values() for revision in revisions
    )
    paths = (
        observe_repository_paths(
            jj_client=context.jj_client,
            descendant_of=anchors,
            exclude_trunk_descendants=True,
            include_working_copies=True,
            state=state,
        ).paths
        if anchors
        else ()
    )
    reviews, stacks = await asyncio.gather(
        observe_reviews(
            change_ids=change_ids,
            context=context,
            github_client=github,
            include_remote_targets=False,
            local_revisions_snapshot=all_copies,
            remote_name=remote_name,
        ),
        observe_github_stacks(github=github),
    )
    commit_ids = _observation_commit_ids(reviews)
    return GlobalSyncFacts(
        ancestries=classify_commit_ancestries(
            commit_ids=commit_ids,
            context=context,
            trunk_commit_id=trunk_commit_id,
        ),
        local_copies=local_copies,
        paths=paths,
        reviews=reviews,
        stacks=stacks,
    )


def build_global_convergence_plan(
    *,
    facts: GlobalSyncFacts,
    repository: github_resolution.GithubRepoAddress,
    state: ReviewState,
) -> GlobalConvergencePlan:
    blocked: list[tuple[TrackedReview, Message]] = []
    finishes: list[ReviewFinishPlan] = []
    heads: list[str] = []
    tracked_pulls = frozenset(
        identity.pr_number
        for identity in state.review_identities.values()
        if identity.repository_key == repository.repository_key
    )
    for candidate in state.tracked_reviews():
        reason, finish, candidate_heads = _classify_global_candidate(
            candidate=candidate,
            facts=facts,
            repository=repository,
            tracked_pull_numbers=tracked_pulls,
        )
        heads.extend(candidate_heads)
        if reason is not None:
            blocked.append((candidate, reason))
        if finish is not None:
            finishes.append(finish)
    return GlobalConvergencePlan(
        blocked=tuple(blocked),
        finishes=tuple(finishes),
        sync_change_ids=tuple(dict.fromkeys(heads)),
    )


def _classify_global_candidate(
    *,
    candidate: TrackedReview,
    facts: GlobalSyncFacts,
    repository: github_resolution.GithubRepoAddress,
    tracked_pull_numbers: frozenset[int],
) -> tuple[Message | None, ReviewFinishPlan | None, tuple[str, ...]]:
    ancestry = facts.ancestries[candidate.submitted_baseline.commit_id]
    pull_request = facts.reviews.reviews[candidate.change_id].pull_request
    evidence: TrunkEvidenceKind | None = None
    reason: Message = ""
    if pull_request is not None:
        evidence, reason = classify_proven_kind(
            ancestries=facts.ancestries,
            candidate=candidate,
            pull_request=pull_request,
            repository=repository,
        )
    heads = _candidate_path_heads(candidate, facts=facts)
    affected = ancestry == "on_trunk" or evidence == "rewritten"
    if affected:
        return _affected_candidate_plan(
            candidate=candidate,
            evidence=evidence,
            facts=facts,
            heads=heads,
            pull_request=pull_request,
            reason=reason,
            tracked_pulls=tracked_pull_numbers,
        )
    if pull_request is None:
        return t"GitHub no longer reports PR #{candidate.review_identity.pr_number}", None, ()
    if ancestry == "unresolved":
        return "the submitted commit is unavailable locally", None, ()
    if reason and (
        pull_request.normalize_state().state != "open"
        or not candidate.review_identity.matches_pull_request(pull_request)
    ):
        return reason, None, ()
    return None, None, ()


def _affected_candidate_plan(
    *,
    candidate: TrackedReview,
    evidence: TrunkEvidenceKind | None,
    facts: GlobalSyncFacts,
    heads: tuple[str, ...] | None,
    pull_request: GithubPullRequest | None,
    reason: Message,
    tracked_pulls: frozenset[int],
) -> tuple[Message | None, ReviewFinishPlan | None, tuple[str, ...]]:
    if heads is None:
        return "local history is not a supported stack", None, ()
    if heads:
        return None, None, heads
    if pull_request is None:
        return t"GitHub no longer reports PR #{candidate.review_identity.pr_number}", None, ()
    if evidence is None:
        return reason, None, ()
    stack_reason, historical = _detached_stack_blocker(
        candidate=candidate,
        stacks=facts.stacks,
        tracked_pull_numbers=tracked_pulls,
    )
    if stack_reason is not None:
        return stack_reason, None, ()
    finish = (
        SkipReviewFinish(candidate)
        if evidence == "rewritten" or historical or pull_request.normalize_state().state != "open"
        else FinishReview(candidate, pull_request)
    )
    return None, finish, ()


def _candidate_path_heads(
    candidate: TrackedReview, *, facts: GlobalSyncFacts
) -> tuple[str, ...] | None:
    copies = {revision.commit_id for revision in facts.local_copies[candidate.change_id]}
    if not copies:
        return ()
    heads = tuple(
        path.stack.head.change_id
        for path in facts.paths
        if any(revision.commit_id in copies for revision in path.stack.revisions)
    )
    return heads or None


def _detached_stack_blocker(
    *,
    candidate: TrackedReview,
    stacks: tuple[GithubStack, ...],
    tracked_pull_numbers: frozenset[int],
) -> tuple[Message | None, bool]:
    number = candidate.review_identity.pr_number
    matching = tuple(
        member for stack in stacks for member in stack.pull_requests if member.number == number
    )
    if not matching:
        return None, False
    if not matching[0].is_historical:
        return t"GitHub still lists PR #{number} as an active member of its stack", False
    blocked = any(
        number in stack.pull_request_numbers
        and not set(stack.active_pull_request_numbers).isdisjoint(tracked_pull_numbers)
        for stack in stacks
    )
    return (
        (
            t"PR #{number} is in a GitHub stack that still has active members tracked here"
            if blocked
            else None
        ),
        True,
    )


def _observation_commit_ids(observation: RepositoryObservation) -> tuple[str, ...]:
    return tuple(
        commit_id
        for item in observation.reviews.values()
        for commit_id in (
            item.baseline.commit_id if item.baseline is not None else None,
            item.pull_request.merge_commit_sha if item.pull_request is not None else None,
        )
        if commit_id is not None
    )
