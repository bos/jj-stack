from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalRevision
from jj_stack.review.native_sync import (
    NativeSurvivorReview,
    build_selected_native_sync,
)
from jj_stack.review.observation import RepositoryObservation
from jj_stack.review.status import PreparedStatus
from jj_stack.review.trunk_evidence import (
    TrackedReview,
    TrunkEvidenceKind,
    proven_kind,
)
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class OnTrunkChange:
    candidate: TrackedReview
    evidence_kind: TrunkEvidenceKind
    native: bool
    revision: LocalRevision | None


@dataclass(frozen=True, slots=True)
class SelectedConvergencePlan:
    on_trunk: tuple[OnTrunkChange, ...]
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
    # Stated once for the whole selection. Enforcing it per change meant two checks that each
    # missed a population: the evidence path never saw a GitHub-stack survivor, and the survivor
    # path never saw a change whose work is already on trunk.
    ambiguous = tuple(
        revision.change_id
        for revision in selected
        if revision.change_id in observation.duplicate_claim_change_ids
    )
    if ambiguous:
        raise CliError(
            t"Multiple saved changes claim the review for {ui.join(ui.change_id, ambiguous)}.",
            hint=t"Run {ui.cmd('jj-stack list')} to find them, then drop the wrong one with "
            t"{ui.cmd('jj-stack unstack --local')}.",
        )
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
    on_trunk: list[OnTrunkChange] = [
        OnTrunkChange(
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
        candidate = state.tracked_review(revision.change_id)
        evidence_kind = (
            None
            if candidate is None or revision.change_id in native_survivors
            else _trunk_evidence_kind_for(
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
        on_trunk.append(
            OnTrunkChange(
                candidate=candidate,
                evidence_kind=evidence_kind,
                native=False,
                revision=revision,
            )
        )

    _require_no_unpublished_edits(tuple(on_trunk))
    reviewed: list[LocalRevision] = []
    saw_unreviewed = False
    for revision in survivors:
        candidate = state.tracked_review(revision.change_id)
        if candidate is None:
            saw_unreviewed = True
            continue
        if saw_unreviewed:
            raise CliError(
                t"Cannot sync because reviewed {ui.change_id(revision.change_id)} appears "
                t"above an unreviewed change.",
                hint="Submit the intervening change or select a stack that ends below it.",
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
                hint=t"Reattach the intended review with {ui.cmd('jj-stack relink')}, or end "
                t"this review with {ui.cmd('jj-stack unstack --cleanup')} and submit it again.",
            )
        lifecycle = pull_request.normalize_state().state
        if lifecycle != "open":
            raise CliError(
                t"PR #{pull_request.number} for {ui.change_id(candidate.change_id)} is "
                t"{lifecycle}, so sync cannot update that review.",
                hint=t"Reopen it on GitHub, or end this review with "
                t"{ui.cmd('jj-stack unstack --cleanup')} and submit it again.",
            )
        reviewed.append(revision)
    plan = SelectedConvergencePlan(
        on_trunk=tuple(on_trunk),
        native_survivors=tuple(native_survivors.values()),
        reviewed_survivors=tuple(reviewed),
        survivors=tuple(survivors),
    )
    _validate_rebase_scope(context=context, plan=plan)
    return plan


def _trunk_evidence_kind_for(
    *,
    candidate: TrackedReview,
    context: CommandContext,
    observation: RepositoryObservation,
    repository: GithubRepoAddress,
    trunk_commit_id: str,
) -> TrunkEvidenceKind | None:
    observed = observation.reviews[candidate.change_id]
    if observed.identity != candidate.review_identity:
        raise CliError(
            t"Saved PR tracking changed for {ui.change_id(candidate.change_id)}.",
            hint=t"Inspect it with {ui.cmd('jj-stack view')}, then reattach the intended "
            t"review with {ui.cmd('jj-stack relink')}.",
        )
    pull_request = observed.pull_request
    if pull_request is None:
        raise CliError(
            t"GitHub no longer reports PR #{candidate.review_identity.pr_number}.",
            hint=t"Confirm it with {ui.cmd('jj-stack view')}, then reattach an open "
            t"replacement with {ui.cmd('jj-stack relink')}, or end the review with "
            t"{ui.cmd('jj-stack unstack --cleanup')} and submit it again.",
        )
    evidence_kind, reason = proven_kind(
        candidate=candidate,
        context=context,
        pull_request=pull_request,
        repository=repository,
        trunk_commit_id=trunk_commit_id,
    )
    if evidence_kind is None and pull_request.normalize_state().state in {"closed", "merged"}:
        raise CliError(
            t"Cannot remove {ui.change_id(candidate.change_id)}: {reason}.",
            hint="Make GitHub's reported merge commit reachable from trunk, then rerun sync.",
        )
    return evidence_kind


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
    if not plan.on_trunk or not plan.survivors:
        return
    selected_commit_ids = {revision.commit_id for revision in plan.survivors}
    selected_commit_ids.update(
        item.revision.commit_id for item in plan.on_trunk if item.revision is not None
    )
    source_commit_ids = tuple(
        item.revision.commit_id
        if item.revision is not None
        else item.candidate.submitted_baseline.commit_id
        for item in plan.on_trunk
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
    candidate: TrackedReview,
    context: CommandContext,
    plan: SelectedConvergencePlan,
) -> Message | None:
    change = next(item for item in plan.on_trunk if item.candidate == candidate)
    if change.evidence_kind == "exact":
        return None
    ancestor_commit_id = (
        change.revision.commit_id
        if change.revision is not None
        else change.candidate.submitted_baseline.commit_id
    )
    recovery = dependent_path_commands(
        ancestor_commit_id=ancestor_commit_id,
        context=context,
        excluded_change_ids={
            *(item.candidate.change_id for item in plan.on_trunk),
            *(revision.change_id for revision in plan.survivors),
        },
    )
    return None if recovery is None else t"another local stack still depends on it; {recovery}"


def _require_no_unpublished_edits(
    changes: tuple[OnTrunkChange, ...],
) -> None:
    for item in changes:
        if item.revision is not None and item.revision.holds_unpublished_edit(
            (item.candidate.submitted_baseline.commit_id,)
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
