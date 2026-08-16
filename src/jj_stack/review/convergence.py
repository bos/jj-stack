from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubPullRequest, GithubStack, GithubStackPullRequest
from jj_stack.models.review_state import ReviewState, TrackedReview
from jj_stack.models.stack import LocalRevision
from jj_stack.review.convergence_models import (
    AdoptedSurvivor,
    ConvergenceActions,
    FinishReview,
    GithubStackMergePlan,
    GithubStackRebasePlan,
    OnTrunkChange,
    OrdinaryConvergencePlan,
    ReviewFinishPlan,
    SelectedConvergencePlan,
    SkipReviewFinish,
)
from jj_stack.review.github_stack_safety import selected_github_stack
from jj_stack.review.observation import RepositoryObservation
from jj_stack.review.status import PreparedStatus
from jj_stack.review.trunk_evidence import (
    CommitAncestry,
    TrunkEvidenceKind,
    classify_proven_kind,
)


class CheckedOutMergedChangeError(CliError):
    def __init__(self, message, *, workspaces: tuple[str, ...]) -> None:
        super().__init__(message)
        self.workspaces = workspaces


@dataclass(frozen=True, slots=True)
class _NoGithubStack:
    pass


@dataclass(frozen=True, slots=True)
class _GithubStackMerge:
    history: tuple[OnTrunkChange, ...]
    adopted: tuple[AdoptedSurvivor, ...]


@dataclass(frozen=True, slots=True)
class _GithubStackRebase:
    adopted: tuple[AdoptedSurvivor, ...]


type _GithubStackEffect = _NoGithubStack | _GithubStackMerge | _GithubStackRebase


def build_selected_convergence_plan(
    *,
    ancestries: dict[str, CommitAncestry],
    context: CommandContext,
    github_stacks: tuple[GithubStack, ...],
    observation: RepositoryObservation,
    prepared_status: PreparedStatus,
    repository: GithubRepoAddress,
    trunk_branch: str,
) -> SelectedConvergencePlan:
    selected = prepared_status.prepared.stack.revisions
    state = prepared_status.prepared.state
    effect = _classify_github_stack(
        ancestries=ancestries,
        github_stacks=github_stacks,
        observation=observation,
        repository=repository,
        selected=selected,
        state=state,
        trunk_branch=trunk_branch,
        trunk_commit_id=prepared_status.prepared.stack.trunk.commit_id,
    )
    history = effect.history if isinstance(effect, _GithubStackMerge) else ()
    adopted = effect.adopted if not isinstance(effect, _NoGithubStack) else ()
    history_ids = {item.candidate.change_id for item in history}
    active_ids = {item.candidate.change_id for item in adopted}
    on_trunk = list(history)
    survivors: list[LocalRevision] = []
    for revision in (item for item in selected if item.change_id not in history_ids):
        candidate = state.tracked_review(revision.change_id)
        evidence_kind = (
            None
            if candidate is None or revision.change_id in active_ids
            else _trunk_evidence_kind_for(
                ancestries=ancestries,
                candidate=candidate,
                observation=observation,
                repository=repository,
            )
        )
        if candidate is None or evidence_kind is None:
            survivors.append(revision)
            continue
        if survivors:
            raise CliError(
                t"Cannot sync reviewed {ui.change_id(revision.change_id)} because these "
                t"unmerged local changes are its parents: "
                t"{ui.join(lambda item: ui.change_id(item.change_id), tuple(survivors))}. "
                t"The submitted review is already on fetched trunk, so sync cannot decide "
                t"whether those local changes belong before or after it.\n"
                t"Submitted commit: "
                t"{ui.semantic_text(candidate.submitted_baseline.commit_id, 'commit_id')}\n"
                t"Local copy commit: {ui.semantic_text(revision.commit_id, 'commit_id')}\n"
                t"Fetched trunk commit: "
                t"{
                    ui.semantic_text(prepared_status.prepared.stack.trunk.commit_id, 'commit_id')
                }",
                hint=t"Inspect the local and fetched histories with "
                t"{
                    ui.cmd(f"jj log -r 'trunk() | (trunk()..{selected[-1].commit_id})'")
                }. Choose the intended order with {ui.cmd('jj')}; ask an agent to inspect "
                t"this repository and these commit IDs if useful. Then inspect the remaining "
                t"local reviews with {ui.cmd('jj-stack view')}. Run "
                t"{ui.cmd('jj-stack sync <head-change-id>')} for a remaining mutable reviewed "
                t"head, or {ui.cmd('jj-stack cleanup')} if none remains.",
            )
        on_trunk.append(
            OnTrunkChange(
                candidate=candidate,
                evidence_kind=evidence_kind,
                finish=_finish_plan(candidate, observation, evidence_kind == "exact"),
                revision=revision,
            )
        )

    _require_no_unpublished_edits(tuple(on_trunk))
    _require_no_checked_out_merged_changes(tuple(on_trunk), context=context)
    reviewed = _reviewed_survivors(
        survivors=tuple(survivors),
        state=state,
        observation=observation,
        repository=repository,
    )
    local_head = selected[-1]
    working_copy_children = tuple(
        revision.commit_id
        for revision in context.jj_client.query_descendant_revisions((local_head.commit_id,))
        if revision.is_working_copy
        and revision.empty
        and revision.parents == (local_head.commit_id,)
    )
    actions = ConvergenceActions(
        on_trunk=tuple(on_trunk),
        reviewed_survivors=reviewed,
        survivors=tuple(survivors),
        working_copy_children=working_copy_children,
    )
    _require_no_divergent_survivors(actions, adopted=adopted)
    if isinstance(effect, _GithubStackRebase):
        return GithubStackRebasePlan(actions=actions, adopted_survivors=adopted)
    if isinstance(effect, _GithubStackMerge):
        return GithubStackMergePlan(actions=actions, adopted_survivors=adopted)
    return OrdinaryConvergencePlan(actions=actions)


def _reviewed_survivors(
    *,
    survivors: tuple[LocalRevision, ...],
    state: ReviewState,
    observation: RepositoryObservation,
    repository: GithubRepoAddress,
) -> tuple[LocalRevision, ...]:
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
                hint=t"Reattach the intended review with {ui.cmd('jj-stack relink')}, or forget "
                t"the incorrect link with {ui.cmd('jj-stack unstack --local')} before "
                t"submitting again.",
            )
        lifecycle = pull_request.normalize_state().state
        if lifecycle != "open":
            raise CliError(
                t"PR #{pull_request.number} for {ui.change_id(candidate.change_id)} is "
                t"{lifecycle}, so sync cannot update that review.",
                hint=t"Reopen it on GitHub, or run {ui.cmd('jj-stack cleanup')} before "
                t"submitting again.",
            )
        reviewed.append(revision)
    return tuple(reviewed)


def _trunk_evidence_kind_for(
    *,
    ancestries: dict[str, CommitAncestry],
    candidate: TrackedReview,
    observation: RepositoryObservation,
    repository: GithubRepoAddress,
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
            t"replacement with {ui.cmd('jj-stack relink')}, or forget the missing link with "
            t"{ui.cmd('jj-stack unstack --local')} before submitting again.",
        )
    evidence_kind, reason = classify_proven_kind(
        ancestries=ancestries,
        candidate=candidate,
        pull_request=pull_request,
        repository=repository,
    )
    if evidence_kind is None and pull_request.normalize_state().state in {"closed", "merged"}:
        raise CliError(
            t"Cannot remove {ui.change_id(candidate.change_id)}: {reason}.",
            hint="Make GitHub's reported merge commit reachable from trunk, then rerun sync.",
        )
    return evidence_kind


def _require_no_divergent_survivors(
    actions: ConvergenceActions,
    *,
    adopted: tuple[AdoptedSurvivor, ...],
) -> None:
    expected_remote_copies = {item.candidate.change_id for item in adopted}
    for revision in actions.survivors:
        if revision.divergent and revision.change_id not in expected_remote_copies:
            raise CliError(
                t"Cannot rebase remaining {ui.change_id(revision.change_id)} because it has "
                t"multiple visible revisions.",
                hint=t"Resolve the divergence with {ui.cmd('jj')}, then rerun sync for this "
                t"stack.",
            )


def _classify_github_stack(
    *,
    ancestries: dict[str, CommitAncestry],
    github_stacks: tuple[GithubStack, ...],
    observation: RepositoryObservation,
    repository: GithubRepoAddress,
    selected: tuple[LocalRevision, ...],
    state: ReviewState,
    trunk_branch: str,
    trunk_commit_id: str,
) -> _GithubStackEffect:
    selected_by_id = {revision.change_id: revision for revision in selected}
    by_pull = {
        candidate.review_identity.pr_number: candidate
        for candidate in state.tracked_reviews()
        if candidate.review_identity.repository_key == repository.repository_key
    }
    candidates = tuple(
        candidate
        for revision in selected
        if (candidate := state.tracked_review(revision.change_id)) is not None
    )
    pulls = {candidate.review_identity.pr_number for candidate in candidates}
    stack = selected_github_stack(selected_pull_numbers=pulls, stacks=github_stacks)
    if stack is None:
        return _NoGithubStack()
    ordered = tuple(number for number in stack.pull_request_numbers if number in pulls)
    if tuple(candidate.review_identity.pr_number for candidate in candidates) != ordered:
        raise CliError(
            t"Selected reviews do not match GitHub stack #{stack.number}'s ordered members.",
            hint=t"Bring them back into line with {ui.cmd('jj-stack submit')}, or remove the "
            t"grouping with {ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit.",
        )
    merge_mode = _is_stack_merge(stack=stack, by_pull=by_pull, candidates=candidates)
    history: list[OnTrunkChange] = []
    adopted: list[AdoptedSurvivor] = []
    expected_base = trunk_branch
    for member in stack.pull_requests:
        candidate = by_pull.get(member.number)
        if candidate is None:
            continue
        pull_request = _validated_member(candidate, member, observation)
        if member.is_historical:
            revision = selected_by_id.get(candidate.change_id)
            editable = tuple(
                item
                for item in observation.reviews[candidate.change_id].local_revisions
                if not item.immutable
            )
            if revision is None and len(editable) > 1:
                raise CliError(
                    t"Historical stack member {ui.change_id(candidate.change_id)} has more "
                    t"than one editable local revision.",
                    hint=t"Resolve the divergent change with {ui.cmd('jj')}, then rerun sync.",
                )
            kind, reason = classify_proven_kind(
                ancestries=ancestries,
                candidate=candidate,
                pull_request=pull_request,
                repository=repository,
            )
            if kind is None:
                raise CliError(
                    t"Cannot remove the saved link for stack member PR #{member.number}: "
                    t"{reason}.",
                    hint="Make GitHub's merge result reachable from trunk, then rerun sync.",
                )
            history.append(
                OnTrunkChange(
                    candidate,
                    kind,
                    SkipReviewFinish(candidate),
                    revision or (editable[0] if editable else None),
                )
            )
            continue
        local = selected_by_id[candidate.change_id]
        _validate_active_member(
            candidate=candidate,
            expected_base=expected_base,
            merge_mode=merge_mode,
            member=member,
            observation=observation,
            pull_request=pull_request,
            selected_revision=local,
            stack=stack,
        )
        adopted.append(AdoptedSurvivor(candidate, local, member.head.sha))
        expected_base = candidate.review_identity.head_ref
    result = tuple(adopted)
    if not merge_mode:
        if any(
            item.remote_commit_id == item.candidate.submitted_baseline.commit_id
            for item in result
        ):
            raise _unproven_rewrite_error(stack)
        return _GithubStackRebase(result)
    return _GithubStackMerge(tuple(history), result)


def _is_stack_merge(
    *,
    stack: GithubStack,
    by_pull: dict[int, TrackedReview],
    candidates: tuple[TrackedReview, ...],
) -> bool:
    merge_mode = any(member.number in by_pull for member in stack.historical_pull_requests)
    if not merge_mode and (
        stack.historical_pull_requests
        or stack.active_pull_request_numbers
        != tuple(candidate.review_identity.pr_number for candidate in candidates)
    ):
        raise _unproven_rewrite_error(stack)
    return merge_mode


def _validated_member(
    candidate: TrackedReview,
    member: GithubStackPullRequest,
    observation: RepositoryObservation,
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


def _validate_active_member(
    *,
    candidate: TrackedReview,
    expected_base: str,
    merge_mode: bool,
    member: GithubStackPullRequest,
    observation: RepositoryObservation,
    pull_request: GithubPullRequest,
    selected_revision: LocalRevision,
    stack: GithubStack,
) -> None:
    observed = observation.reviews[candidate.change_id]
    expected = {selected_revision.commit_id, member.head.sha}
    if any(
        not item.immutable and item.commit_id not in expected for item in observed.local_revisions
    ):
        raise CliError(
            t"Cannot sync {ui.change_id(candidate.change_id)} because it has another editable "
            t"local revision besides the selected commit.",
            hint=t"Resolve the divergence with {ui.cmd('jj')}, then rerun sync for this stack.",
        )
    if selected_revision.immutable and selected_revision.commit_id != member.head.sha:
        raise CliError(
            t"GitHub still lists PR #{member.number} as active in stack #{stack.number}, but "
            t"{ui.change_id(candidate.change_id)} is already immutable here, so this repository "
            t"cannot tell what GitHub did with it.",
            hint=t"Check GitHub's result with {ui.cmd('jj-stack view')}, then rerun sync once it "
            t"reports the merge.",
        )
    if merge_mode and selected_revision.holds_unpublished_edit(
        (candidate.submitted_baseline.commit_id, member.head.sha)
    ):
        raise CliError(
            t"Cannot sync {ui.change_id(candidate.change_id)} because it has unpublished local "
            t"edits since submit.",
            hint=t"Publish them with {ui.cmd('jj-stack submit')}, or drop them, then rerun sync.",
        )
    if (
        pull_request.head.sha != member.head.sha
        or observed.remote_review_target != member.head.sha
    ):
        raise CliError(
            t"Active stack member PR #{member.number} does not match its reviewed branch.",
            hint=t"Republish the review with {ui.cmd('jj-stack submit')}, then rerun sync.",
        )
    if not merge_mode and pull_request.base.ref != expected_base:
        raise CliError(
            t"PR #{member.number} no longer has the base expected for this stack.",
            hint=t"Restore the stack on GitHub, or run "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit it.",
        )


def _unproven_rewrite_error(stack: GithubStack) -> CliError:
    return CliError(
        t"GitHub stack #{stack.number} changed, but none of its merged members is tracked here "
        t"and the whole active stack was not rebased, so jj-stack cannot determine how GitHub "
        t"changed the reviews.",
        hint=t"Inspect it with {ui.cmd('jj-stack view')}. Restore or resubmit the review "
        t"branches, then rerun sync.",
    )


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


def _finish_plan(
    candidate: TrackedReview,
    observation: RepositoryObservation,
    allowed: bool,
) -> ReviewFinishPlan:
    pull_request = observation.reviews[candidate.change_id].pull_request
    if not allowed or pull_request is None or pull_request.normalize_state().state != "open":
        return SkipReviewFinish(candidate)
    return FinishReview(candidate, pull_request)


def _require_no_checked_out_merged_changes(
    changes: tuple[OnTrunkChange, ...],
    *,
    context: CommandContext,
) -> None:
    for item in changes:
        revision = item.revision
        if revision is None or not revision.is_working_copy:
            continue
        workspaces = revision.working_copy_workspaces
        if not workspaces:
            location = "the current workspace"
        elif len(workspaces) == 1:
            location = t"workspace {ui.code(workspaces[0])}"
        else:
            location = t"workspaces {ui.join(ui.code, workspaces)}"
        raise CheckedOutMergedChangeError(
            t"Cannot remove merged {ui.change_id(item.candidate.change_id)} because it is "
            t"checked out in {location}.",
            workspaces=workspaces,
        )
