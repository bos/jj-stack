"""Pure selected-path convergence planning and dependency checks."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.stack import LocalRevision
from jj_stack.review.landed_evidence import (
    LandedEvidenceKind,
    LandedReviewCandidate,
    candidate_for_change,
    collect_landed_evidence,
)
from jj_stack.review.observation import RepositoryObservation
from jj_stack.review.status import PreparedStatus
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class SelectedLanded:
    """One landed revision in the selected bottom prefix."""

    candidate: LandedReviewCandidate
    evidence_kind: LandedEvidenceKind
    revision: LocalRevision


@dataclass(frozen=True, slots=True)
class SelectedConvergencePlan:
    """Validated selected-path mutations and reviewed resubmit boundary."""

    landed: tuple[SelectedLanded, ...]
    reviewed_survivors: tuple[LocalRevision, ...]
    rewrite_blocker: Message | None
    survivors: tuple[LocalRevision, ...]


def build_selected_convergence_plan(
    *,
    context: CommandContext,
    observation: RepositoryObservation,
    prepared_status: PreparedStatus,
    repository: GithubRepoAddress,
) -> SelectedConvergencePlan:
    selected = prepared_status.prepared.stack.revisions
    state = prepared_status.prepared.state
    landed: list[SelectedLanded] = []
    survivors: list[LocalRevision] = []
    rewrite_blocker: Message | None = None
    seen_survivor = False
    for revision in selected:
        candidate = candidate_for_change(state, revision.change_id)
        evidence_kind = (
            None
            if candidate is None
            else _selected_landed_kind(
                candidate=candidate,
                context=context,
                observation=observation,
                repository=repository,
                trunk_commit_id=prepared_status.prepared.stack.trunk.commit_id,
            )
        )
        if candidate is None or evidence_kind is None:
            seen_survivor = True
            survivors.append(revision)
            continue
        if seen_survivor:
            raise CliError(
                t"Landed {ui.change_id(revision.change_id)} appears above an unlanded change "
                t"in this stack.",
                hint="Run sync separately for each affected stack.",
            )
        if revision.commit_id != candidate.submitted_baseline.commit_id:
            rewrite_blocker = (
                t"Cannot remove landed {ui.change_id(revision.change_id)} because it has "
                t"unpublished local edits since submit"
            )
        landed.append(
            SelectedLanded(
                candidate=candidate,
                evidence_kind=evidence_kind,
                revision=revision,
            )
        )

    reviewed: list[LocalRevision] = []
    saw_unreviewed = False
    for revision in survivors:
        candidate = candidate_for_change(state, revision.change_id)
        if candidate is None:
            saw_unreviewed = True
            continue
        if saw_unreviewed:
            raise CliError(
                t"Cannot sync because reviewed {ui.change_id(revision.change_id)} appears "
                t"above an unreviewed change.",
                hint="Submit the intervening change or select a stack that ends below it.",
            )
        if revision.change_id in observation.duplicate_claim_change_ids:
            raise CliError(t"Multiple saved changes claim the review for {revision.change_id}.")
        pull_request = observation.reviews[revision.change_id].pull_request
        _validate_surviving_review(
            candidate=candidate,
            pull_request=pull_request,
            repository=repository,
        )
        reviewed.append(revision)
    plan = SelectedConvergencePlan(
        landed=tuple(landed),
        reviewed_survivors=tuple(reviewed),
        rewrite_blocker=rewrite_blocker,
        survivors=tuple(survivors),
    )
    _validate_rebase_scope(context=context, plan=plan)
    return plan


def _selected_landed_kind(
    *,
    candidate: LandedReviewCandidate,
    context: CommandContext,
    observation: RepositoryObservation,
    repository: GithubRepoAddress,
    trunk_commit_id: str,
) -> LandedEvidenceKind | None:
    observed = observation.reviews[candidate.change_id]
    if observed.identity != candidate.review_identity:
        raise CliError(t"Saved PR tracking changed for {ui.change_id(candidate.change_id)}.")
    if candidate.change_id in observation.duplicate_claim_change_ids:
        raise CliError(t"Multiple saved changes claim the review for {candidate.change_id}.")
    pull_request = observed.pull_request
    if pull_request is None:
        raise CliError(t"GitHub no longer reports PR #{candidate.review_identity.pr_number}.")
    exact, rewritten = collect_landed_evidence(
        candidate=candidate,
        context=context,
        pull_request=pull_request,
        repository=repository,
        trunk_commit_id=trunk_commit_id,
    )
    if exact.state == "landed":
        return "exact"
    if rewritten.state == "landed":
        return "rewritten"
    if pull_request.normalize_state().state in {"closed", "merged"}:
        reason = (
            rewritten.reason
            or exact.reason
            or "neither its submitted commit nor GitHub's merge commit is on trunk"
        )
        raise CliError(
            t"Cannot remove {ui.change_id(candidate.change_id)}: {reason}.",
            hint="Make GitHub's reported merge commit reachable from trunk, then rerun sync.",
        )
    return None


def _validate_surviving_review(
    *,
    candidate: LandedReviewCandidate,
    pull_request: GithubPullRequest | None,
    repository: GithubRepoAddress,
) -> None:
    if pull_request is None:
        raise CliError(t"GitHub no longer reports PR #{candidate.review_identity.pr_number}.")
    identity = candidate.review_identity
    if (
        identity.repository_key != repository.repository_key
        or pull_request.number != identity.pr_number
        or pull_request.head.ref != identity.head_ref
        or pull_request.head.label != f"{identity.head_owner}:{identity.head_ref}"
        or pull_request.normalize_state().state != "open"
    ):
        raise CliError(
            t"The pull request no longer matches saved tracking for "
            t"{ui.change_id(candidate.change_id)}."
        )


def _validate_rebase_scope(
    *,
    context: CommandContext,
    plan: SelectedConvergencePlan,
) -> None:
    for revision in plan.survivors:
        if revision.conflict or revision.divergent or len(revision.parents) != 1:
            raise CliError(
                t"The changes remaining after the merge are not linear at "
                t"{ui.change_id(revision.change_id)}."
            )
    if not plan.landed or not plan.survivors:
        return
    selected_commit_ids = {
        revision.commit_id
        for revision in (*plan.survivors, *(item.revision for item in plan.landed))
    }
    outside = tuple(
        revision
        for revision in context.jj_client.query_descendant_revisions(
            tuple(revision.commit_id for revision in plan.survivors)
        )
        if revision.commit_id not in selected_commit_ids
        and not revision.immutable
        and not (
            revision.is_working_copy
            and revision.empty
            and revision.parents == (plan.survivors[-1].commit_id,)
        )
    )
    if outside:
        heads = ui.join(lambda revision: ui.change_id(revision.change_id), outside)
        raise CliError(
            t"Other local changes depend on this stack: {heads}.",
            hint="Select each affected stack and run sync explicitly.",
        )


def selected_rebase_revision_ids(
    *,
    context: CommandContext,
    plan: SelectedConvergencePlan,
) -> tuple[str, ...]:
    revision_ids = [revision.commit_id for revision in plan.survivors]
    if not plan.landed:
        return ()
    head = plan.survivors[-1] if plan.survivors else plan.landed[-1].revision
    for revision in context.jj_client.query_descendant_revisions((head.commit_id,)):
        if revision.is_working_copy and revision.empty and revision.parents == (head.commit_id,):
            revision_ids.append(revision.commit_id)
    return tuple(revision_ids)


def rewritten_retirement_blocker(
    *,
    candidate: LandedReviewCandidate,
    context: CommandContext,
    plan: SelectedConvergencePlan,
) -> Message | None:
    landed = next(item for item in plan.landed if item.candidate == candidate)
    if landed.evidence_kind == "exact":
        return None
    landed_commit_ids = {landed.revision.commit_id for landed in plan.landed}
    recovery = dependent_path_commands(
        ancestor_commit_id=landed.revision.commit_id,
        context=context,
        excluded_commit_ids=landed_commit_ids,
    )
    return None if recovery is None else t"another local stack still depends on it; {recovery}"


def dependent_path_commands(
    *,
    ancestor_commit_id: str,
    context: CommandContext,
    excluded_commit_ids: set[str] | None = None,
) -> Message | None:
    excluded = excluded_commit_ids or set()
    dependents = tuple(
        revision
        for revision in context.jj_client.query_descendant_revisions((ancestor_commit_id,))
        if revision.commit_id not in excluded
        and not (revision.is_working_copy and revision.empty)
    )
    if not dependents:
        return None
    if any(
        revision.conflict or revision.divergent or len(revision.parents) != 1
        for revision in dependents
    ):
        revisions = ui.join(lambda item: ui.change_id(item.change_id), dependents)
        return t"repair these non-linear dependent changes, then rerun sync: {revisions}"
    dependent_commit_ids = {revision.commit_id for revision in dependents}
    parent_commit_ids = {parent for revision in dependents for parent in revision.parents}
    heads = tuple(
        revision
        for revision in dependents
        if revision.commit_id in dependent_commit_ids - parent_commit_ids
    )
    return t"run {ui.join(
        lambda revision: ui.cmd(f'jj-stack sync {revision.change_id}'), heads or dependents
    )}"
