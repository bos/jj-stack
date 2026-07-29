from __future__ import annotations

from dataclasses import dataclass

import jj_stack.review.observation as review_observation
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._github_stack_support import resolve_github_stack_support
from jj_stack.commands._native_stack_safety import selected_native_stack
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubPullRequest, GithubStack, GithubStackPullRequest
from jj_stack.models.review_state import ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.trunk_evidence import (
    TrackedReview,
    TrunkEvidenceKind,
    proven_kind,
)


@dataclass(frozen=True, slots=True)
class NativeHistoricalReview:
    candidate: TrackedReview
    evidence_kind: TrunkEvidenceKind
    revision: LocalRevision | None


@dataclass(frozen=True, slots=True)
class NativeSurvivorReview:
    candidate: TrackedReview
    remote_head_commit_id: str


async def observe_native_stacks(
    *,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
) -> tuple[GithubStack, ...]:
    """Read the current native resources when GitHub supports them."""

    try:
        support = await resolve_github_stack_support(
            github_client=github,
            state_store=context.state_store,
            persist=not dry_run,
        )
        if not support.supported:
            return ()
        return (
            support.observed_stacks
            if support.observed_stacks is not None
            else await github.list_stacks()
        )
    except GithubClientError as error:
        raise CliError(
            "Could not inspect native GitHub stack membership.",
            hint=t"Resolve the GitHub error above, then rerun the command.",
        ) from error


async def resolve_selected_native_observation(
    *,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
    initial: review_observation.RepositoryObservation,
    remote_name: str,
    repository: GithubRepoAddress,
    selected: tuple[LocalRevision, ...],
    trunk_branch: str,
) -> tuple[review_observation.RepositoryObservation, tuple[GithubStack, ...]]:
    state = context.state_store.load()
    selected_change_ids = {revision.change_id for revision in selected}
    has_unselected_tracking = any(
        change_id not in selected_change_ids
        and (candidate := state.tracked_review(change_id)) is not None
        and candidate.review_identity.repository_key == repository.repository_key
        for change_id in state.review_identities
    )
    if not has_unselected_tracking and not any(
        _changed_review(initial.reviews[revision.change_id]) for revision in selected
    ):
        return initial, ()
    stacks = await observe_native_stacks(context=context, dry_run=dry_run, github=github)

    selected_pull_numbers = {
        identity.pr_number
        for revision in selected
        if (identity := state.review_identities.get(revision.change_id)) is not None
    }
    affected = tuple(
        stack
        for stack in stacks
        if not selected_pull_numbers.isdisjoint(stack.pull_request_numbers)
    )
    resource_pull_numbers = {
        pull_number for stack in affected for pull_number in stack.pull_request_numbers
    }
    change_ids = tuple(
        change_id
        for change_id, identity in state.review_identities.items()
        if identity.repository_key == repository.repository_key
        and identity.pr_number in resource_pull_numbers
        and change_id in state.submitted_baselines
    )
    observation = await review_observation.observe_reviews(
        change_ids=tuple(dict.fromkeys((*change_ids, *initial.reviews))),
        context=context,
        github_client=github,
        remote_name=remote_name,
        trunk_branch=trunk_branch,
    )
    return observation, stacks if any(map(_changed_review, observation.reviews.values())) else ()


def _changed_review(observed: review_observation.ReviewObservation) -> bool:
    pull_request = observed.pull_request
    if pull_request is None or (baseline := observed.baseline) is None:
        return False
    return (
        pull_request.normalize_state().state == "merged"
        or pull_request.head.sha != baseline.commit_id
        or observed.remote_review_target != baseline.commit_id
        or observed.local_revision is not None
        and observed.local_revision.immutable
    )


def build_selected_native_sync(
    *,
    context: CommandContext,
    native_stacks: tuple[GithubStack, ...],
    observation: review_observation.RepositoryObservation,
    repository: GithubRepoAddress,
    selected: tuple[LocalRevision, ...],
    state: ReviewState,
    trunk_commit_id: str,
) -> tuple[tuple[NativeHistoricalReview, ...], tuple[NativeSurvivorReview, ...]]:
    selected_by_change_id = {revision.change_id: revision for revision in selected}
    candidates_by_pull = {
        candidate.review_identity.pr_number: candidate
        for change_id in state.review_identities
        if (candidate := state.tracked_review(change_id)) is not None
        and candidate.review_identity.repository_key == repository.repository_key
    }
    selected_candidates = tuple(
        candidate
        for revision in selected
        if (candidate := state.tracked_review(revision.change_id)) is not None
    )
    selected_pull_numbers = {
        candidate.review_identity.pr_number for candidate in selected_candidates
    }
    stack = selected_native_stack(
        selected_pull_numbers=selected_pull_numbers,
        stacks=native_stacks,
    )
    if stack is None:
        return (), ()
    selected_resource_numbers = tuple(
        pull_number
        for pull_number in stack.pull_request_numbers
        if pull_number in selected_pull_numbers
    )
    if (
        tuple(candidate.review_identity.pr_number for candidate in selected_candidates)
        != selected_resource_numbers
    ):
        raise CliError(
            t"Selected reviews do not match GitHub stack #{stack.number}'s ordered members.",
            hint=t"Bring them back into line with {ui.cmd('jj-stack submit')}, or dissolve the "
            t"stack with {ui.cmd(f'gh stack unstack {stack.number}')} and resubmit.",
        )
    historical: list[NativeHistoricalReview] = []
    survivors: list[NativeSurvivorReview] = []
    _require_history(stack, set(candidates_by_pull))
    for member in stack.pull_requests:
        candidate = candidates_by_pull.get(member.number)
        if candidate is None:
            continue
        pull_request = _validated_member_pull_request(
            candidate=candidate,
            member=member,
            observation=observation,
        )
        if member.is_historical:
            historical.append(
                _historical_review(
                    candidate=candidate,
                    context=context,
                    member=member,
                    pull_request=pull_request,
                    repository=repository,
                    revision=observation.reviews[candidate.change_id].local_revision,
                    trunk_commit_id=trunk_commit_id,
                )
            )
            continue
        if selected_by_change_id[candidate.change_id].immutable:
            raise CliError(
                t"Native member PR #{member.number} is not terminally merged.",
                hint=t"Check GitHub's result with {ui.cmd('jj-stack view')}, then "
                t"rerun sync once it reports the merge.",
            )
        if selected_by_change_id[candidate.change_id].holds_unpublished_edit(
            (candidate.submitted_baseline.commit_id, member.head.sha)
        ):
            raise CliError(
                t"Cannot sync {ui.change_id(candidate.change_id)} because it has unpublished "
                t"local edits since submit.",
                hint=t"Publish them with {ui.cmd('jj-stack submit')}, or drop them, then rerun "
                t"sync.",
            )
        observed = observation.reviews[candidate.change_id]
        # A closed or draft active member is still an affected survivor; only its branch has
        # to match here. Convergence decides which surviving reviews can still be updated.
        if (
            pull_request.head.sha != member.head.sha
            or observed.remote_review_target != member.head.sha
        ):
            raise CliError(
                t"Active native member PR #{member.number} does not match its reviewed branch.",
                hint=t"Republish the review with {ui.cmd('jj-stack submit')}, then rerun sync.",
            )
        survivors.append(
            NativeSurvivorReview(
                candidate=candidate,
                remote_head_commit_id=member.head.sha,
            )
        )
    return tuple(historical), tuple(survivors)


def _require_history(stack: GithubStack, tracked: set[int]) -> None:
    if not any(member.number in tracked for member in stack.historical_pull_requests):
        raise CliError(
            t"GitHub stack #{stack.number} rewrote a review head, but no merged member of it "
            t"is tracked here to prove that transition.",
            hint=t"Inspect the stack with {ui.cmd('jj-stack view')}, then attach the "
            t"merged review with {ui.cmd('jj-stack relink')} if it belongs to this repository.",
        )


def _historical_review(
    *,
    candidate: TrackedReview,
    context: CommandContext,
    member: GithubStackPullRequest,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
    revision: LocalRevision | None,
    trunk_commit_id: str,
) -> NativeHistoricalReview:
    if member.head.sha != candidate.submitted_baseline.commit_id:
        raise CliError(
            t"Historical native member PR #{member.number} no longer reports its submitted head.",
            hint=t"Inspect it with {ui.cmd('jj-stack view')}, then reattach the "
            t"intended review with {ui.cmd('jj-stack relink')}.",
        )
    if pull_request.normalize_state().state != "merged":
        raise CliError(
            t"Native member PR #{member.number} is not terminally merged.",
            hint=t"Check GitHub's result with {ui.cmd('jj-stack view')}, then rerun "
            t"sync once it reports the merge.",
        )
    evidence_kind, reason = proven_kind(
        candidate=candidate,
        context=context,
        pull_request=pull_request,
        repository=repository,
        trunk_commit_id=trunk_commit_id,
    )
    if evidence_kind is None:
        raise CliError(
            t"Cannot retire native member PR #{member.number}: {reason}.",
            hint=t"Make GitHub's merge result reachable from trunk, then rerun sync.",
        )
    return NativeHistoricalReview(
        candidate=candidate,
        evidence_kind=evidence_kind,
        revision=revision,
    )


def _validated_member_pull_request(
    *,
    candidate: TrackedReview,
    member: GithubStackPullRequest,
    observation: review_observation.RepositoryObservation,
) -> GithubPullRequest:
    observed = observation.reviews.get(candidate.change_id)
    pull_request = observed.pull_request if observed is not None else None
    identity = candidate.review_identity
    if (
        observed is None
        or observed.identity != identity
        or candidate.change_id in observation.duplicate_claim_change_ids
        or pull_request is None
        or not identity.matches_pull_request(pull_request)
        or pull_request.number != member.number
        or pull_request.head.ref != member.head.ref
    ):
        raise CliError(
            t"Native member PR #{member.number} no longer matches its saved review identity.",
            hint=t"Reattach it with {ui.cmd('jj-stack relink')}, or end the review with "
            t"{ui.cmd('jj-stack unstack --cleanup')} and submit it again.",
        )
    return pull_request
