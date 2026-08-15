from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import jj_stack.review.observation as review_observation
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._github_stack_safety import selected_github_stack
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
class GithubStackHistoryReview:
    candidate: TrackedReview
    evidence_kind: TrunkEvidenceKind
    revision: LocalRevision | None


@dataclass(frozen=True, slots=True)
class GithubStackActiveReview:
    candidate: TrackedReview
    remote_head_commit_id: str


@dataclass(frozen=True, slots=True)
class GithubStackRewrite:
    mode: Literal["merge", "rebase"]
    history: tuple[GithubStackHistoryReview, ...]
    active: tuple[GithubStackActiveReview, ...]


async def observe_github_stacks(
    *,
    github: GithubClient,
) -> tuple[GithubStack, ...]:
    """Read the current GitHub stack resources."""

    try:
        return await github.list_stacks()
    except GithubClientError as error:
        raise CliError(
            "Could not inspect GitHub stack membership.",
            hint=t"Resolve the GitHub error above, then rerun the command.",
        ) from error


async def resolve_selected_github_stack_observation(
    *,
    context: CommandContext,
    github: GithubClient,
    initial: review_observation.RepositoryObservation,
    remote_name: str,
    repository: GithubRepoAddress,
    selected: tuple[LocalRevision, ...],
    stacks: tuple[GithubStack, ...],
) -> tuple[review_observation.RepositoryObservation, tuple[GithubStack, ...], bool]:
    state = context.state_store.load()
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
    tracked_pull_numbers = {
        identity.pr_number
        for identity in state.review_identities.values()
        if identity.repository_key == repository.repository_key
    }
    if not any(
        _changed_review(initial.reviews[revision.change_id], include_remote_target=False)
        for revision in selected
    ) and (resource_pull_numbers & tracked_pull_numbers).issubset(selected_pull_numbers):
        return initial, (), False
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
    )
    changed = any(_changed_review(item) for item in observation.reviews.values())
    return observation, stacks if changed else (), changed


def _changed_review(
    observed: review_observation.ReviewObservation,
    *,
    include_remote_target: bool = True,
) -> bool:
    pull_request = observed.pull_request
    if (baseline := observed.baseline) is None:
        return False
    if pull_request is None:
        return True
    return (
        pull_request.normalize_state().state == "merged"
        or pull_request.head.sha != baseline.commit_id
        or (include_remote_target and observed.remote_review_target != baseline.commit_id)
        or any(revision.immutable for revision in observed.local_revisions)
    )


def build_selected_github_stack_rewrite(
    *,
    context: CommandContext,
    github_stacks: tuple[GithubStack, ...],
    observation: review_observation.RepositoryObservation,
    repository: GithubRepoAddress,
    selected: tuple[LocalRevision, ...],
    state: ReviewState,
    trunk_branch: str,
    trunk_commit_id: str,
) -> GithubStackRewrite | None:
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
    stack = selected_github_stack(
        selected_pull_numbers=selected_pull_numbers,
        stacks=github_stacks,
    )
    if stack is None:
        return None
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
            hint=t"Bring them back into line with {ui.cmd('jj-stack submit')}, or remove the "
            t"grouping with {ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit.",
        )
    historical: list[GithubStackHistoryReview] = []
    active: list[GithubStackActiveReview] = []
    mode = _rewrite_mode(stack, set(candidates_by_pull))
    if mode == "rebase" and stack.active_pull_request_numbers != tuple(
        candidate.review_identity.pr_number for candidate in selected_candidates
    ):
        raise _unproven_rewrite_error(stack)
    expected_base = trunk_branch
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
            selected_revision = selected_by_change_id.get(candidate.change_id)
            editable = tuple(
                revision
                for revision in observation.reviews[candidate.change_id].local_revisions
                if not revision.immutable
            )
            if selected_revision is None and len(editable) > 1:
                raise CliError(
                    t"Historical stack member {ui.change_id(candidate.change_id)} has more "
                    t"than one editable local revision.",
                    hint=t"Resolve the divergent change with {ui.cmd('jj')}, then rerun sync.",
                )
            historical.append(
                _historical_review(
                    candidate=candidate,
                    context=context,
                    member=member,
                    pull_request=pull_request,
                    repository=repository,
                    revision=selected_revision or (editable[0] if editable else None),
                    trunk_commit_id=trunk_commit_id,
                )
            )
            continue
        active.append(
            _active_review(
                candidate=candidate,
                expected_base=expected_base,
                member=member,
                mode=mode,
                observation=observation,
                pull_request=pull_request,
                selected_revision=selected_by_change_id[candidate.change_id],
                stack=stack,
            )
        )
        expected_base = candidate.review_identity.head_ref
    if mode == "rebase" and any(
        item.remote_head_commit_id == item.candidate.submitted_baseline.commit_id
        for item in active
    ):
        raise _unproven_rewrite_error(stack)
    return GithubStackRewrite(mode=mode, history=tuple(historical), active=tuple(active))


def _active_review(
    *,
    candidate: TrackedReview,
    expected_base: str,
    member: GithubStackPullRequest,
    mode: Literal["merge", "rebase"],
    observation: review_observation.RepositoryObservation,
    pull_request: GithubPullRequest,
    selected_revision: LocalRevision,
    stack: GithubStack,
) -> GithubStackActiveReview:
    if selected_revision.immutable and selected_revision.commit_id != member.head.sha:
        raise CliError(
            t"GitHub still lists PR #{member.number} as active in stack #{stack.number}, "
            t"but {ui.change_id(candidate.change_id)} is already immutable here, so this "
            t"repository cannot tell what GitHub did with it.",
            hint=t"Check GitHub's result with {ui.cmd('jj-stack view')}, then "
            t"rerun sync once it reports the merge.",
        )
    if (
        selected_revision.holds_unpublished_edit(
            (candidate.submitted_baseline.commit_id, member.head.sha)
        )
        and mode == "merge"
    ):
        raise CliError(
            t"Cannot sync {ui.change_id(candidate.change_id)} because it has unpublished "
            t"local edits since submit.",
            hint=t"Publish them with {ui.cmd('jj-stack submit')}, or drop them, then rerun sync.",
        )
    observed = observation.reviews[candidate.change_id]
    # A closed or draft active member is still an affected survivor; only its branch has to match
    # here. Convergence decides which surviving reviews can still be updated.
    if (
        pull_request.head.sha != member.head.sha
        or observed.remote_review_target != member.head.sha
    ):
        raise CliError(
            t"Active stack member PR #{member.number} does not match its reviewed branch.",
            hint=t"Republish the review with {ui.cmd('jj-stack submit')}, then rerun sync.",
        )
    if mode == "rebase" and pull_request.base.ref != expected_base:
        raise CliError(
            t"PR #{member.number} no longer has the base expected for this stack.",
            hint=t"Restore the stack on GitHub, or run "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit it.",
        )
    return GithubStackActiveReview(
        candidate=candidate,
        remote_head_commit_id=member.head.sha,
    )


def _rewrite_mode(stack: GithubStack, tracked: set[int]) -> Literal["merge", "rebase"]:
    if any(member.number in tracked for member in stack.historical_pull_requests):
        return "merge"
    if stack.historical_pull_requests:
        raise _unproven_rewrite_error(stack)
    return "rebase"


def _unproven_rewrite_error(stack: GithubStack) -> CliError:
    return CliError(
        t"GitHub stack #{stack.number} changed, but none of its merged members is tracked "
        t"here and the whole active stack was not rebased, so jj-stack cannot determine how "
        t"GitHub changed the reviews.",
        hint=t"Inspect it with {ui.cmd('jj-stack view')}. Restore or resubmit the review "
        t"branches, then rerun sync.",
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
) -> GithubStackHistoryReview:
    evidence_kind, reason = proven_kind(
        candidate=candidate,
        context=context,
        pull_request=pull_request,
        repository=repository,
        trunk_commit_id=trunk_commit_id,
    )
    if evidence_kind is None:
        raise CliError(
            t"Cannot remove the saved link for stack member PR #{member.number}: {reason}.",
            hint=t"Make GitHub's merge result reachable from trunk, then rerun sync.",
        )
    return GithubStackHistoryReview(
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
        or pull_request is None
        or not identity.matches_pull_request(pull_request)
        or pull_request.head.ref != member.head.ref
    ):
        raise CliError(
            t"Stack member PR #{member.number} no longer matches its saved review identity.",
            hint=t"Reattach it with {ui.cmd('jj-stack relink')}, or forget the incorrect link "
            t"with {ui.cmd('jj-stack unstack --local')} before submitting again.",
        )
    return pull_request
