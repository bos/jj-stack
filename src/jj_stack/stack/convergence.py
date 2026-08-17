from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubPR, GithubStack, GithubStackPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR, TrackingState
from jj_stack.stack.convergence_models import (
    AdoptedSurvivor,
    ConvergenceActions,
    FinishPR,
    GithubStackMergePlan,
    GithubStackRebasePlan,
    OnTrunkChange,
    OrdinaryConvergencePlan,
    PRFinishPlan,
    SelectedConvergencePlan,
    SkipPRFinish,
)
from jj_stack.stack.github_stack_safety import selected_github_stack
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.stack.status import PreparedStatus
from jj_stack.stack.trunk_evidence import (
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
    observation: RepoFacts,
    prepared_status: PreparedStatus,
    repo: GithubRepoAddress,
    trunk_branch: str,
) -> SelectedConvergencePlan:
    selected = prepared_status.prepared.stack.changes
    state = prepared_status.prepared.state
    effect = _classify_github_stack(
        ancestries=ancestries,
        github_stacks=github_stacks,
        observation=observation,
        repo=repo,
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
    survivors: list[LocalCommit] = []
    for change in (item for item in selected if item.change_id not in history_ids):
        candidate = state.tracked_pr(change.change_id)
        evidence_kind = (
            None
            if candidate is None or change.change_id in active_ids
            else _trunk_evidence_kind_for(
                ancestries=ancestries,
                candidate=candidate,
                observation=observation,
                repo=repo,
            )
        )
        if candidate is None or evidence_kind is None:
            survivors.append(change)
            continue
        if survivors:
            raise CliError(
                t"Cannot sync submitted {ui.change_id(change.change_id)} because these "
                t"unmerged local changes are its parents: "
                t"{ui.join(lambda item: ui.change_id(item.change_id), tuple(survivors))}. "
                t"The submitted change is already on fetched trunk, so sync cannot decide "
                t"whether those local changes belong before or after it.\n"
                t"Submitted commit: "
                t"{ui.semantic_text(candidate.submitted_baseline.commit_id, 'commit_id')}\n"
                t"Local copy commit: {ui.semantic_text(change.commit_id, 'commit_id')}\n"
                t"Fetched trunk commit: "
                t"{
                    ui.semantic_text(prepared_status.prepared.stack.trunk.commit_id, 'commit_id')
                }",
                hint=t"Inspect the local and fetched histories with "
                t"{
                    ui.cmd(f"jj log -r 'trunk() | (trunk()..{selected[-1].commit_id})'")
                }. Choose the intended order with {ui.cmd('jj')}; ask an agent to inspect "
                t"this repo and these commit IDs if useful. Then inspect the remaining "
                t"local pull requests with {ui.cmd('jj-stack view')}. Run "
                t"{ui.cmd('jj-stack sync <head-change-id>')} for a remaining mutable submitted "
                t"head, or {ui.cmd('jj-stack cleanup')} if none remains.",
            )
        on_trunk.append(
            OnTrunkChange(
                candidate=candidate,
                evidence_kind=evidence_kind,
                finish=_finish_plan(candidate, observation, evidence_kind == "exact"),
                change=change,
            )
        )

    _require_no_unpublished_edits(tuple(on_trunk))
    _require_no_checked_out_merged_changes(tuple(on_trunk), context=context)
    submitted = _submitted_survivors(
        survivors=tuple(survivors),
        state=state,
        observation=observation,
        repo=repo,
    )
    local_head = selected[-1]
    working_copy_children = tuple(
        commit.commit_id
        for commit in context.jj_client.query_descendant_commits((local_head.commit_id,))
        if commit.is_working_copy and commit.empty and commit.parents == (local_head.commit_id,)
    )
    actions = ConvergenceActions(
        on_trunk=tuple(on_trunk),
        submitted_survivors=submitted,
        survivors=tuple(survivors),
        working_copy_children=working_copy_children,
    )
    _require_no_divergent_survivors(actions, adopted=adopted)
    if isinstance(effect, _GithubStackRebase):
        return GithubStackRebasePlan(actions=actions, adopted_survivors=adopted)
    if isinstance(effect, _GithubStackMerge):
        return GithubStackMergePlan(actions=actions, adopted_survivors=adopted)
    return OrdinaryConvergencePlan(actions=actions)


def _submitted_survivors(
    *,
    survivors: tuple[LocalCommit, ...],
    state: TrackingState,
    observation: RepoFacts,
    repo: GithubRepoAddress,
) -> tuple[LocalCommit, ...]:
    submitted: list[LocalCommit] = []
    saw_unsubmitted = False
    for change in survivors:
        candidate = state.tracked_pr(change.change_id)
        if candidate is None:
            saw_unsubmitted = True
            continue
        if saw_unsubmitted:
            raise CliError(
                t"Cannot sync because submitted {ui.change_id(change.change_id)} appears "
                t"above an unsubmitted change.",
                hint="Submit the intervening change or select a stack that ends below it.",
            )
        pr = observation.prs[change.change_id].pr
        identity = candidate.pr_identity
        if pr is None or identity.repo_key != repo.repo_key or not identity.matches_pr(pr):
            raise CliError(
                t"The pull request no longer matches saved tracking for "
                t"{ui.change_id(candidate.change_id)}.",
                hint=t"Reattach the intended PR with {ui.cmd('jj-stack relink')}, or forget "
                t"the incorrect link with {ui.cmd('jj-stack unstack --local')} before "
                t"submitting again.",
            )
        lifecycle = pr.normalize_state().state
        if lifecycle != "open":
            raise CliError(
                t"PR #{pr.number} for {ui.change_id(candidate.change_id)} is "
                t"{lifecycle}, so sync cannot update that PR.",
                hint=t"Reopen it on GitHub, or run {ui.cmd('jj-stack cleanup')} before "
                t"submitting again.",
            )
        submitted.append(change)
    return tuple(submitted)


def _trunk_evidence_kind_for(
    *,
    ancestries: dict[str, CommitAncestry],
    candidate: TrackedPR,
    observation: RepoFacts,
    repo: GithubRepoAddress,
) -> TrunkEvidenceKind | None:
    observed = observation.prs[candidate.change_id]
    if observed.identity != candidate.pr_identity:
        raise CliError(
            t"Saved PR tracking changed for {ui.change_id(candidate.change_id)}.",
            hint=t"Inspect it with {ui.cmd('jj-stack view')}, then reattach the intended "
            t"PR with {ui.cmd('jj-stack relink')}.",
        )
    pr = observed.pr
    if pr is None:
        raise CliError(
            t"GitHub no longer reports PR #{candidate.pr_identity.pr_number}.",
            hint=t"Confirm it with {ui.cmd('jj-stack view')}, then reattach an open "
            t"replacement with {ui.cmd('jj-stack relink')}, or forget the missing link with "
            t"{ui.cmd('jj-stack unstack --local')} before submitting again.",
        )
    evidence_kind, reason = classify_proven_kind(
        ancestries=ancestries,
        candidate=candidate,
        pr=pr,
        repo=repo,
    )
    if evidence_kind is None and pr.normalize_state().state in {"closed", "merged"}:
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
    for change in actions.survivors:
        if change.divergent and change.change_id not in expected_remote_copies:
            raise CliError(
                t"Cannot rebase remaining {ui.change_id(change.change_id)} because it has "
                t"multiple visible commits.",
                hint=t"Resolve the divergence with {ui.cmd('jj')}, then rerun sync for this "
                t"stack.",
            )


def _classify_github_stack(
    *,
    ancestries: dict[str, CommitAncestry],
    github_stacks: tuple[GithubStack, ...],
    observation: RepoFacts,
    repo: GithubRepoAddress,
    selected: tuple[LocalCommit, ...],
    state: TrackingState,
    trunk_branch: str,
    trunk_commit_id: str,
) -> _GithubStackEffect:
    selected_by_id = {change.change_id: change for change in selected}
    by_pr = {
        candidate.pr_identity.pr_number: candidate
        for candidate in state.tracked_prs()
        if candidate.pr_identity.repo_key == repo.repo_key
    }
    candidates = tuple(
        candidate
        for change in selected
        if (candidate := state.tracked_pr(change.change_id)) is not None
    )
    prs = {candidate.pr_identity.pr_number for candidate in candidates}
    stack = selected_github_stack(selected_pr_numbers=prs, stacks=github_stacks)
    if stack is None:
        return _NoGithubStack()
    ordered = tuple(number for number in stack.pr_numbers if number in prs)
    if tuple(candidate.pr_identity.pr_number for candidate in candidates) != ordered:
        raise CliError(
            t"Selected PRs do not match GitHub stack #{stack.number}'s ordered members.",
            hint=t"Bring them back into line with {ui.cmd('jj-stack submit')}, or remove the "
            t"grouping with {ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit.",
        )
    merge_mode = _is_stack_merge(stack=stack, by_pr=by_pr, candidates=candidates)
    history: list[OnTrunkChange] = []
    adopted: list[AdoptedSurvivor] = []
    expected_base = trunk_branch
    for member in stack.prs:
        candidate = by_pr.get(member.number)
        if candidate is None:
            continue
        pr = _validated_member(candidate, member, observation)
        if member.is_historical:
            change = selected_by_id.get(candidate.change_id)
            mutable_copies = tuple(
                item
                for item in observation.prs[candidate.change_id].local_commits
                if not item.immutable
            )
            if change is None and len(mutable_copies) > 1:
                raise CliError(
                    t"Historical stack member {ui.change_id(candidate.change_id)} has more "
                    t"than one mutable local copy.",
                    hint=t"Resolve the divergent change with {ui.cmd('jj')}, then rerun sync.",
                )
            kind, reason = classify_proven_kind(
                ancestries=ancestries,
                candidate=candidate,
                pr=pr,
                repo=repo,
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
                    SkipPRFinish(candidate),
                    change or (mutable_copies[0] if mutable_copies else None),
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
            pr=pr,
            selected_change=local,
            stack=stack,
        )
        adopted.append(AdoptedSurvivor(candidate, local, member.head.sha))
        expected_base = candidate.pr_identity.head_ref
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
    by_pr: dict[int, TrackedPR],
    candidates: tuple[TrackedPR, ...],
) -> bool:
    merge_mode = any(member.number in by_pr for member in stack.historical_prs)
    if not merge_mode and (
        stack.historical_prs
        or stack.active_pr_numbers
        != tuple(candidate.pr_identity.pr_number for candidate in candidates)
    ):
        raise _unproven_rewrite_error(stack)
    return merge_mode


def _validated_member(
    candidate: TrackedPR,
    member: GithubStackPR,
    observation: RepoFacts,
) -> GithubPR:
    observed = observation.prs.get(candidate.change_id)
    pr = observed.pr if observed is not None else None
    identity = candidate.pr_identity
    if (
        observed is None
        or observed.identity != identity
        or pr is None
        or not identity.matches_pr(pr)
        or pr.head.ref != member.head.ref
    ):
        raise CliError(
            t"Stack member PR #{member.number} no longer matches its saved PR identity.",
            hint=t"Reattach it with {ui.cmd('jj-stack relink')}, or forget the incorrect link "
            t"with {ui.cmd('jj-stack unstack --local')} before submitting again.",
        )
    return pr


def _validate_active_member(
    *,
    candidate: TrackedPR,
    expected_base: str,
    merge_mode: bool,
    member: GithubStackPR,
    observation: RepoFacts,
    pr: GithubPR,
    selected_change: LocalCommit,
    stack: GithubStack,
) -> None:
    observed = observation.prs[candidate.change_id]
    expected = {selected_change.commit_id, member.head.sha}
    if any(
        not item.immutable and item.commit_id not in expected for item in observed.local_commits
    ):
        raise CliError(
            t"Cannot sync {ui.change_id(candidate.change_id)} because it has more than one "
            t"mutable local copy.",
            hint=t"Resolve the divergence with {ui.cmd('jj')}, then rerun sync for this stack.",
        )
    if selected_change.immutable and selected_change.commit_id != member.head.sha:
        raise CliError(
            t"GitHub still lists PR #{member.number} as active in stack #{stack.number}, but "
            t"{ui.change_id(candidate.change_id)} is already immutable here, so this repo "
            t"cannot tell what GitHub did with it.",
            hint=t"Check GitHub's result with {ui.cmd('jj-stack view')}, then rerun sync once it "
            t"reports the merge.",
        )
    if merge_mode and selected_change.holds_unpublished_edit(
        (candidate.submitted_baseline.commit_id, member.head.sha)
    ):
        raise CliError(
            t"Cannot sync {ui.change_id(candidate.change_id)} because it has unpublished local "
            t"edits since submit.",
            hint=t"Publish them with {ui.cmd('jj-stack submit')}, or drop them, then rerun sync.",
        )
    if pr.head.sha != member.head.sha or observed.remote_pr_branch_target != member.head.sha:
        raise CliError(
            t"Active stack member PR #{member.number} does not match its PR branch.",
            hint=t"Republish the PR with {ui.cmd('jj-stack submit')}, then rerun sync.",
        )
    if not merge_mode and pr.base.ref != expected_base:
        raise CliError(
            t"PR #{member.number} no longer has the base expected for this stack.",
            hint=t"Restore the stack on GitHub, or run "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit it.",
        )


def _unproven_rewrite_error(stack: GithubStack) -> CliError:
    return CliError(
        t"GitHub stack #{stack.number} changed, but none of its merged members is tracked here "
        t"and the whole active stack was not rebased, so jj-stack cannot determine how GitHub "
        t"changed the pull requests.",
        hint=t"Inspect it with {ui.cmd('jj-stack view')}. Restore or resubmit the PR "
        t"branches, then rerun sync.",
    )


def _require_no_unpublished_edits(
    changes: tuple[OnTrunkChange, ...],
) -> None:
    for item in changes:
        if item.change is not None and item.change.holds_unpublished_edit(
            (item.candidate.submitted_baseline.commit_id,)
        ):
            raise CliError(
                t"Cannot remove merged {ui.change_id(item.candidate.change_id)} because it has "
                t"unpublished local edits since submit.",
                hint=t"Publish them with {ui.cmd('jj-stack submit')}, or drop them, then rerun "
                t"sync.",
            )


def _finish_plan(
    candidate: TrackedPR,
    observation: RepoFacts,
    allowed: bool,
) -> PRFinishPlan:
    pr = observation.prs[candidate.change_id].pr
    if not allowed or pr is None or pr.normalize_state().state != "open":
        return SkipPRFinish(candidate)
    return FinishPR(candidate, pr)


def _require_no_checked_out_merged_changes(
    changes: tuple[OnTrunkChange, ...],
    *,
    context: CommandContext,
) -> None:
    for item in changes:
        change = item.change
        if change is None or not change.is_working_copy:
            continue
        workspaces = change.working_copy_workspaces
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
