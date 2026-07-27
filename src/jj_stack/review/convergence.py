from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalRevision
from jj_stack.review.landed_evidence import (
    LandedEvidenceKind,
    LandedReviewCandidate,
    candidate_for_change,
    collect_landed_evidence,
    holds_unpublished_edit,
)
from jj_stack.review.native_sync import (
    NativeSurvivorReview,
    build_selected_native_sync,
)
from jj_stack.review.observation import RepositoryObservation
from jj_stack.review.status import PreparedStatus
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class SelectedLanded:
    candidate: LandedReviewCandidate
    evidence_kind: LandedEvidenceKind
    native: bool
    revision: LocalRevision | None


@dataclass(frozen=True, slots=True)
class SelectedConvergencePlan:
    landed: tuple[SelectedLanded, ...]
    native_survivors: tuple[NativeSurvivorReview, ...]
    reviewed_survivors: tuple[LocalRevision, ...]
    survivors: tuple[LocalRevision, ...]


def build_selected_convergence_plan(
    *,
    context: CommandContext,
    native_stacks: tuple[GithubStack, ...],
    observation: RepositoryObservation,
    prepared_status: PreparedStatus,
    repository: GithubRepoAddress,
) -> SelectedConvergencePlan:
    selected = prepared_status.prepared.stack.revisions
    state = prepared_status.prepared.state
    native_history, native_active = build_selected_native_sync(
        context=context,
        native_stacks=native_stacks,
        observation=observation,
        repository=repository,
        selected=selected,
        state=state,
        trunk_commit_id=prepared_status.prepared.stack.trunk.commit_id,
    )
    native_historical = {item.candidate.change_id: item for item in native_history}
    native_survivors = {item.candidate.change_id: item for item in native_active}
    landed: list[SelectedLanded] = [
        SelectedLanded(
            candidate=item.candidate,
            evidence_kind=item.evidence_kind,
            native=True,
            revision=item.revision,
        )
        for item in native_historical.values()
    ]
    survivors: list[LocalRevision] = []
    seen_survivor = False
    for revision in filter(
        lambda item: item.change_id not in native_historical,
        selected,
    ):
        candidate = candidate_for_change(state, revision.change_id)
        evidence_kind = (
            None
            if candidate is None or revision.change_id in native_survivors
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
                t"Merged {ui.change_id(revision.change_id)} appears above a change that has not "
                t"merged in this stack.",
                hint="Run jj-stack sync separately for each affected stack.",
            )
        landed.append(
            SelectedLanded(
                candidate=candidate,
                evidence_kind=evidence_kind,
                native=False,
                revision=revision,
            )
        )

    _require_no_landed_local_edits(tuple(landed))
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
            raise CliError(
                t"Multiple saved changes claim the review for {revision.change_id}.",
                hint=t"Run {ui.cmd('jj-stack list')} to find them, then drop the wrong one with "
                t"{ui.cmd('jj-stack unstack --local')}.",
            )
        pull_request = observation.reviews[revision.change_id].pull_request
        identity = candidate.review_identity
        if (
            pull_request is None
            or identity.repository_key != repository.repository_key
            or not identity.matches_pull_request(pull_request)
        ):
            raise CliError(
                t"The pull request no longer matches saved tracking for "
                t"{ui.change_id(candidate.change_id)}.",
                hint=t"Reattach the intended review with {ui.cmd('jj-stack relink')}, or replace "
                t"it using {ui.cmd(f'jj-stack submit --restart {candidate.change_id}')}.",
            )
        lifecycle = pull_request.normalize_state().state
        if lifecycle != "open":
            raise CliError(
                t"PR #{pull_request.number} for {ui.change_id(candidate.change_id)} is "
                t"{lifecycle}, so sync cannot update that review.",
                hint=t"Reopen it on GitHub, or replace it using "
                t"{ui.cmd(f'jj-stack submit --restart {candidate.change_id}')}.",
            )
        reviewed.append(revision)
    plan = SelectedConvergencePlan(
        landed=tuple(landed),
        native_survivors=tuple(native_survivors.values()),
        reviewed_survivors=tuple(reviewed),
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
        raise CliError(
            t"Saved PR tracking changed for {ui.change_id(candidate.change_id)}.",
            hint=t"Inspect it with {ui.cmd('jj-stack view --fetch')}, then reattach the intended "
            t"review with {ui.cmd('jj-stack relink')}.",
        )
    if candidate.change_id in observation.duplicate_claim_change_ids:
        raise CliError(
            t"Multiple saved changes claim the review for {candidate.change_id}.",
            hint=t"Run {ui.cmd('jj-stack list')} to find them, then drop the wrong one with "
            t"{ui.cmd('jj-stack unstack --local')}.",
        )
    pull_request = observed.pull_request
    if pull_request is None:
        raise CliError(
            t"GitHub no longer reports PR #{candidate.review_identity.pr_number}.",
            hint=t"Confirm it with {ui.cmd('jj-stack view --fetch')}, then reattach an open "
            t"replacement with {ui.cmd('jj-stack relink')} or open fresh reviews with "
            t"{ui.cmd('jj-stack submit --restart')}.",
        )
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


def _validate_rebase_scope(
    *,
    context: CommandContext,
    plan: SelectedConvergencePlan,
) -> None:
    for revision in plan.survivors:
        if revision.conflict or revision.divergent or len(revision.parents) != 1:
            raise CliError(
                t"The changes remaining after the merge are not linear at "
                t"{ui.change_id(revision.change_id)}.",
                hint=t"Resolve the conflict or divergence with {ui.cmd('jj')}, then rerun sync "
                t"for this stack.",
            )
    if not plan.landed or not plan.survivors:
        return
    selected_commit_ids = {revision.commit_id for revision in plan.survivors}
    selected_commit_ids.update(
        item.revision.commit_id for item in plan.landed if item.revision is not None
    )
    source_commit_ids = tuple(
        item.revision.commit_id
        if item.revision is not None
        else item.candidate.submitted_baseline.commit_id
        for item in plan.landed
    )
    selected_head = plan.survivors[-1]
    outside = tuple(
        revision
        for revision in context.jj_client.query_descendant_revisions(source_commit_ids)
        if revision.commit_id not in selected_commit_ids
        and not revision.hidden
        and not revision.immutable
        and not (
            revision.is_working_copy
            and revision.empty
            and revision.parents == (selected_head.commit_id,)
        )
    )
    if outside:
        heads = ui.join(lambda revision: ui.change_id(revision.change_id), outside)
        raise CliError(
            t"Other local changes depend on this stack: {heads}.",
            hint="Select each affected stack and run jj-stack sync explicitly.",
        )


def rewritten_retirement_blocker(
    *,
    candidate: LandedReviewCandidate,
    context: CommandContext,
    plan: SelectedConvergencePlan,
) -> Message | None:
    landed = next(item for item in plan.landed if item.candidate == candidate)
    if landed.evidence_kind == "exact":
        return None
    ancestor_commit_id = (
        landed.revision.commit_id
        if landed.revision is not None
        else landed.candidate.submitted_baseline.commit_id
    )
    recovery = dependent_path_commands(
        ancestor_commit_id=ancestor_commit_id,
        context=context,
        excluded_change_ids={
            *(item.candidate.change_id for item in plan.landed),
            *(revision.change_id for revision in plan.survivors),
        },
    )
    return None if recovery is None else t"another local stack still depends on it; {recovery}"


def _require_no_landed_local_edits(
    landed: tuple[SelectedLanded, ...],
) -> None:
    for item in landed:
        if holds_unpublished_edit(
            published_commit_ids=(item.candidate.submitted_baseline.commit_id,),
            revision=item.revision,
        ):
            raise CliError(
                t"Cannot remove merged {ui.change_id(item.candidate.change_id)} because it has "
                t"unpublished local edits since submit.",
                hint=t"Publish them with {ui.cmd('jj-stack submit')}, or drop them, then rerun "
                t"sync.",
            )


def dependent_path_commands(
    *,
    ancestor_commit_id: str,
    context: CommandContext,
    excluded_change_ids: set[str] | None = None,
) -> Message | None:
    excluded_changes = excluded_change_ids or set()
    dependents = tuple(
        revision
        for revision in context.jj_client.query_descendant_revisions((ancestor_commit_id,))
        if revision.change_id not in excluded_changes
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
    return t"run {
        ui.join(
            lambda revision: ui.cmd(f'jj-stack sync {revision.change_id}'), heads or dependents
        )
    }"
