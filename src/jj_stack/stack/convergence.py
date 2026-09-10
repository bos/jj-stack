from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import CommitId, short_change_id
from jj_stack.models.github import GithubPR, GithubStack, GithubStackPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR, TrackingState
from jj_stack.stack.change_state import (
    BranchDisagrees,
    BranchMissing,
    Closed,
    Landed,
    Merged,
    PRHeadMoved,
    Stop,
    WithPR,
    classify,
    stop_error,
    trunk_evidence_reason,
)
from jj_stack.stack.convergence_models import (
    ConvergenceActions,
    GithubStackMergePlan,
    GithubStackRebasePlan,
    OnTrunkChange,
    OrdinaryConvergencePlan,
    RewrittenPRChange,
    SelectedConvergencePlan,
)
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.github_stack_safety import selected_github_stack
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.stack.preparation import PreparedLocalStack
from jj_stack.stack.trunk_evidence import CommitAncestry


class CheckedOutMergedChangeError(CliError):
    def __init__(self, message, *, workspaces: tuple[str, ...]) -> None:
        super().__init__(message)
        self.workspaces = workspaces


@dataclass(frozen=True, slots=True)
class _GithubStackMerge:
    history: tuple[OnTrunkChange, ...]
    adopted: tuple[RewrittenPRChange, ...]
    merge_result_commit_id: CommitId | None


@dataclass(frozen=True, slots=True)
class _GithubStackRebase:
    adopted: tuple[RewrittenPRChange, ...]


type _GithubStackEffect = _GithubStackMerge | _GithubStackRebase | None


def build_selected_convergence_plan(
    *,
    ancestries: dict[str, CommitAncestry],
    github_stacks: tuple[GithubStack, ...],
    head_children: tuple[LocalCommit, ...],
    observation: RepoFacts,
    prepared: PreparedLocalStack,
    trunk_branch: str,
) -> SelectedConvergencePlan:
    selected = prepared.stack.changes
    state = prepared.state
    effect = _classify_github_stack(
        ancestries=ancestries,
        github_stacks=github_stacks,
        observation=observation,
        selected=selected,
        state=state,
        trunk_branch=trunk_branch,
    )
    history = effect.history if isinstance(effect, _GithubStackMerge) else ()
    adopted = effect.adopted if effect is not None else ()
    history_ids = {item.change_id for item in history}
    active_ids = {item.change_id for item in adopted}
    on_trunk = list(history)
    remaining_changes: list[LocalCommit] = []
    surviving_prs = {item.change_id: item.pr for item in adopted}
    rerun = f"jj-stack sync {short_change_id(selected[-1].change_id)}"
    for change in (item for item in selected if item.change_id not in history_ids):
        candidate = state.prs.get(change.change_id)
        if candidate is None or change.change_id in active_ids:
            remaining_changes.append(change)
            continue
        change_state = _member_state(
            change_id=change.change_id,
            ancestries=ancestries,
            observation=observation,
            rerun=rerun,
            selected=change,
        )
        if isinstance(change_state, Closed):
            raise _closed_error(change_state)
        if isinstance(change_state, Merged):
            raise CliError(
                t"Cannot remove {ui.change_id(change.change_id)}: "
                t"{trunk_evidence_reason(change_state)}.",
                hint=t"Check that {ui.revset('trunk()')} selects the branch the PR merged "
                t"into, then rerun {ui.cmd(rerun)}.",
            )
        if not isinstance(change_state, Landed):
            remaining_changes.append(change)
            surviving_prs[change.change_id] = change_state.pr
            continue
        evidence_kind = change_state.evidence
        if remaining_changes:
            raise CliError(
                t"Cannot sync submitted {ui.change_id(change.change_id)} because these "
                t"unmerged local changes are its parents: "
                t"{ui.join(lambda item: ui.change_id(item.change_id), tuple(remaining_changes))}"
                t". The submitted change is already on trunk, so jj-stack cannot decide "
                t"whether those local changes belong before or after it.\n"
                t"Submitted commit: "
                t"{ui.semantic_text(candidate.submitted_baseline.commit_id, 'commit_id')}\n"
                t"Local copy commit: {ui.semantic_text(change.commit_id, 'commit_id')}\n"
                t"Trunk commit: "
                t"{ui.semantic_text(prepared.stack.trunk.commit_id, 'commit_id')}",
                hint=t"Inspect the local and fetched histories with "
                t"{
                    ui.cmd(f"jj log -r 'trunk() | (trunk()..{selected[-1].commit_id})'")
                }, and put the unmerged changes where you want them with {ui.cmd('jj')}. Then "
                t"check the remaining pull requests with {ui.cmd('jj-stack view')}, and run "
                t"{ui.cmd('jj-stack sync <head-change-id>')} for a stack that still has "
                t"submitted changes, or {ui.cmd('jj-stack cleanup')} if none remains.",
            )
        pr = observation.prs[change.change_id].pr
        on_trunk.append(
            OnTrunkChange(
                change_id=change.change_id,
                candidate=candidate,
                evidence_kind=evidence_kind,
                close_pr=(
                    pr
                    if evidence_kind == "exact"
                    and isinstance(pr, GithubPR)
                    and pr.state == "open"
                    else None
                ),
                change=change,
            )
        )

    _require_no_unpublished_edits(tuple(on_trunk))
    _require_no_checked_out_merged_changes(tuple(on_trunk))
    submitted = _remaining_submitted_prs(
        remaining_changes=tuple(remaining_changes), prs=surviving_prs
    )
    local_head = selected[-1]
    working_copy_children = tuple(
        commit
        for commit in head_children
        if commit.is_working_copy and commit.empty and commit.parents == (local_head.commit_id,)
    )
    actions = ConvergenceActions(
        on_trunk=tuple(on_trunk),
        remaining_prs=submitted,
        remaining_changes=tuple(remaining_changes),
        working_copy_children=working_copy_children,
    )
    _require_no_divergent_remaining_changes(actions, adopted=adopted)
    if isinstance(effect, _GithubStackRebase):
        return GithubStackRebasePlan(actions=actions, rewritten_changes=adopted)
    if isinstance(effect, _GithubStackMerge):
        return GithubStackMergePlan(
            actions=actions,
            rewritten_changes=adopted,
            # Without a reported merge result, the trunk tip is the only commit left to expect;
            # the import still verifies the chain against it.
            expected_parent_commit_id=effect.merge_result_commit_id
            or prepared.stack.trunk.commit_id,
        )
    return OrdinaryConvergencePlan(actions=actions)


def _remaining_submitted_prs(
    *,
    remaining_changes: tuple[LocalCommit, ...],
    prs: dict[str, GithubPR],
) -> dict[str, GithubPR]:
    """Return the remaining submitted PRs; unsubmitted changes must come after them."""

    submitted: dict[str, GithubPR] = {}
    saw_unsubmitted = False
    for change in remaining_changes:
        if (pr := prs.get(change.change_id)) is None:
            saw_unsubmitted = True
            continue
        if saw_unsubmitted:
            raise CliError(
                t"Cannot sync because submitted {ui.change_id(change.change_id)} appears "
                t"above an unsubmitted change.",
                hint=t"Submit the complete stack with {ui.cmd('jj-stack submit HEAD')}, or "
                t"select a stack that ends below the unsubmitted change.",
            )
        submitted[change.change_id] = pr
    return submitted


def _member_state(
    *,
    ancestries: dict[str, CommitAncestry],
    change_id: str,
    observation: RepoFacts,
    rerun: str,
    member: GithubStackPR | None = None,
    selected: LocalCommit | None = None,
) -> WithPR:
    """Classify one tracked change with its trunk evidence, stopping on a broken saved link."""

    observed = observation.prs[change_id]
    state = classify(observed, ancestries=ancestries, selected=selected)
    # GitHub itself moves the heads of a stack's active members when it merges or rebases the
    # stack; `_validate_active_member` and the commit and ancestry checks validate those changes.
    # Any other PR branch that moved or disappeared stops sync before it rewrites anything.
    github_moved = (
        member is not None
        and not member.is_historical
        and isinstance(state, (BranchDisagrees, BranchMissing, PRHeadMoved))
    )
    if isinstance(state, Stop) and not github_moved:
        raise stop_error(state, rerun=rerun)
    if member is not None and state.pr.head.ref != member.head.ref:
        pr_label = format_pr_label(member.number, repo=observation.repo)
        raise CliError(
            t"{pr_label} no longer matches the saved pull request link for "
            t"{ui.change_id(change_id)}.",
            hint=t"Relink it with {ui.cmd(f'jj-stack relink PR {short_change_id(change_id)}')}, "
            t"or forget the selected stack's links with "
            t"{ui.cmd(f'jj-stack unstack --local {short_change_id(change_id)}')} before "
            t"submitting again.",
        )
    return state


def _closed_error(state: Closed) -> CliError:
    pr_label = format_pr_label(state.pr.number, url=state.pr.html_url)
    return CliError(
        t"{pr_label} for {ui.change_id(state.change_id)} is closed, so jj-stack cannot update "
        t"that PR.",
        hint=t"Reopen it on GitHub, or run "
        t"{ui.cmd(f'jj-stack cleanup --pull-request {state.pr.number}')} before the next submit.",
    )


def _require_no_divergent_remaining_changes(
    actions: ConvergenceActions,
    *,
    adopted: tuple[RewrittenPRChange, ...],
) -> None:
    expected_remote_copies = {item.change_id for item in adopted}
    for change in actions.remaining_changes:
        if change.divergent and change.change_id not in expected_remote_copies:
            raise divergent_change_error(change.change_id)


def divergent_change_error(change_id: str) -> CliError:
    return CliError(
        t"Cannot rebase remaining {ui.change_id(change_id)} because it has multiple visible "
        t"commits.",
        hint=divergence_recovery_hint(
            change_id,
            retry=t"rerun {ui.cmd('jj-stack sync HEAD')} for this stack",
        ),
    )


def _classify_github_stack(
    *,
    ancestries: dict[str, CommitAncestry],
    github_stacks: tuple[GithubStack, ...],
    observation: RepoFacts,
    selected: tuple[LocalCommit, ...],
    state: TrackingState,
    trunk_branch: str,
) -> _GithubStackEffect:
    selected_by_id: dict[str, LocalCommit] = {change.change_id: change for change in selected}
    by_pr = {
        candidate.pr_identity.pr_number: change_id
        for change_id, candidate in sorted(state.prs.items())
    }
    selected_prs = tuple(
        candidate.pr_identity.pr_number
        for change in selected
        if (candidate := state.prs.get(change.change_id)) is not None
    )
    stack = selected_github_stack(observation.repo, selected_prs, github_stacks)
    if stack is None:
        return None
    # A selected PR outside the stack, such as a child submitted with --base, is not compared.
    members = tuple(number for number in selected_prs if number in stack.pr_numbers)
    if members != tuple(number for number in stack.pr_numbers if number in members):
        raise CliError(
            t"The local PR order differs from GitHub stack #{stack.number}.",
            hint=t"Update GitHub with "
            t"{ui.cmd(f'jj-stack submit {short_change_id(selected[-1].change_id)}')}, or remove "
            t"the GitHub stack with {ui.cmd(f'jj-stack unstack --stack {stack.number}')} and "
            t"resubmit.",
        )
    merge_mode = _is_stack_merge(stack=stack, by_pr=by_pr)
    history: list[OnTrunkChange] = []
    adopted: list[RewrittenPRChange] = []
    expected_base = trunk_branch
    merge_result: CommitId | None = None
    rerun = f"jj-stack sync {short_change_id(selected[-1].change_id)}"
    for member in stack.prs:
        change_id = by_pr.get(member.number)
        if change_id is None:
            continue
        candidate = state.prs[change_id]
        member_state = _member_state(
            change_id=change_id,
            ancestries=ancestries,
            observation=observation,
            rerun=rerun,
            member=member,
            selected=selected_by_id.get(change_id),
        )
        pr = member_state.pr
        if member.is_historical:
            history.append(
                _historical_member(
                    candidate=candidate,
                    change_id=change_id,
                    member_state=member_state,
                    observation=observation,
                    selected=selected_by_id.get(change_id),
                )
            )
            merge_result = pr.merge_commit_sha
            continue
        local = selected_by_id[change_id]
        if isinstance(member_state, Closed):
            raise _closed_error(member_state)
        if pr.state == "merged":
            raise CliError(
                t"PR #{pr.number} is merged, but GitHub stack #{stack.number} still lists "
                t"it as active.",
                hint="Wait for GitHub to update the stack, then rerun sync.",
            )
        _validate_active_member(
            expected_base=expected_base,
            merge_mode=merge_mode,
            member=member,
            observation=observation,
            pr=pr,
            selected_change=local,
            stack=stack,
        )
        adopted.append(RewrittenPRChange(change_id, candidate, local, pr))
        expected_base = candidate.pr_identity.head_ref
    result = tuple(adopted)
    if not merge_mode:
        if any(
            item.pr.head.sha == item.candidate.submitted_baseline.commit_id for item in result
        ):
            raise _unmatched_rewrite_error(stack)
        return _GithubStackRebase(result)
    return _GithubStackMerge(tuple(history), result, merge_result)


def _historical_member(
    *,
    candidate: TrackedPR,
    change_id: str,
    member_state: WithPR,
    observation: RepoFacts,
    selected: LocalCommit | None,
) -> OnTrunkChange:
    """Turn a merged stack member into its on-trunk entry, or stop when it cannot be removed."""

    mutable_copies = tuple(
        item for item in observation.prs[change_id].local if not item.immutable
    )
    if selected is None and len(mutable_copies) > 1:
        raise CliError(
            t"Merged change {ui.change_id(change_id)} from this stack has more than "
            t"one mutable local copy.",
            hint=divergence_recovery_hint(
                change_id,
                retry=t"rerun {ui.cmd('jj-stack sync HEAD')}",
            ),
        )
    if not isinstance(member_state, Landed):
        pr_label = format_pr_label(member_state.pr.number, url=member_state.pr.html_url)
        raise CliError(
            t"Cannot remove the saved link for merged {pr_label}: "
            t"{trunk_evidence_reason(member_state)}.",
            hint=t"Check that {ui.revset('trunk()')} selects the branch the PR merged into, "
            t"then rerun {ui.cmd('jj-stack sync HEAD')}.",
        )
    return OnTrunkChange(
        change_id,
        candidate,
        member_state.evidence,
        None,
        selected or (mutable_copies[0] if mutable_copies else None),
    )


def _is_stack_merge(*, stack: GithubStack, by_pr: dict[int, str]) -> bool:
    merge_mode = any(member.number in by_pr for member in stack.historical_prs)
    if stack.historical_prs and not merge_mode:
        raise _unmatched_rewrite_error(stack)
    return merge_mode


def _validate_active_member(
    *,
    expected_base: str,
    merge_mode: bool,
    member: GithubStackPR,
    observation: RepoFacts,
    pr: GithubPR,
    selected_change: LocalCommit,
    stack: GithubStack,
) -> None:
    change_id = selected_change.change_id
    observed = observation.prs[change_id]
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    expected = {selected_change.commit_id, member.head.sha}
    if any(not item.immutable and item.commit_id not in expected for item in observed.local):
        raise CliError(
            t"Cannot sync {ui.change_id(change_id)} because it has more than one "
            t"mutable local copy.",
            hint=divergence_recovery_hint(
                change_id,
                retry=t"rerun {ui.cmd('jj-stack sync HEAD')} for this stack",
            ),
        )
    if selected_change.immutable and selected_change.commit_id != member.head.sha:
        raise CliError(
            t"GitHub still lists {pr_label} as active in stack #{stack.number}, but its local "
            t"change {ui.change_id(change_id)} is immutable and differs from GitHub's commit.",
            hint=t"Check the PR with {ui.cmd(f'jj-stack view {short_change_id(change_id)}')}. "
            t"Once GitHub reports the merge, rerun {ui.cmd('jj-stack sync HEAD')}.",
        )
    if pr.head.sha != member.head.sha or observed.remote_target != member.head.sha:
        raise CliError(
            t"{pr_label}, its PR branch, and GitHub stack #{stack.number} point to different "
            t"commits.",
            hint=t"Check the stack with {ui.cmd(f'jj-stack view {short_change_id(change_id)}')}, "
            t"update it with {ui.cmd('jj-stack submit HEAD')}, then rerun "
            t"{ui.cmd('jj-stack sync HEAD')}.",
        )
    if not merge_mode and pr.base.ref != expected_base:
        raise CliError(
            t"{pr_label} no longer has the base expected for this stack.",
            hint=t"Restore the stack on GitHub, or run "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit it.",
        )


def _unmatched_rewrite_error(stack: GithubStack) -> CliError:
    return CliError(
        t"GitHub stack #{stack.number} changed, but jj-stack cannot verify a merge or a rebase "
        t"of the complete stack from the PRs tracked here.",
        hint=t"Check the stack with {ui.cmd('jj-stack view HEAD')}. Restore or resubmit its PR "
        t"branches, then rerun {ui.cmd('jj-stack sync HEAD')}.",
    )


def _require_no_unpublished_edits(changes: tuple[OnTrunkChange, ...]) -> None:
    for item in changes:
        local, baseline = item.change, item.candidate.submitted_baseline.commit_id
        if local is None or not local.holds_unpublished_edit(baseline):
            continue
        short = short_change_id(local.change_id)
        raise CliError(
            t"Cannot remove merged {ui.change_id(item.change_id)}: its local commit changed "
            t"since submit and is not empty. Removing it could discard local work.",
            hint=t"Run {ui.cmd(f"jj rebase -s {short} -d 'trunk()'")} and rerun "
            t"{ui.cmd('jj-stack sync HEAD')}. If "
            t"{ui.cmd(f'jj diff -r {short}')} still shows changes, move anything still needed "
            t"to another change, then drop this copy with {ui.cmd(f'jj abandon {short}')} and "
            t"rerun {ui.cmd('jj-stack sync HEAD')}, or keep it and forget the selected "
            t"stack's saved links with "
            t"{ui.cmd(f'jj-stack unstack --local {short}')}.",
        )


def _require_no_checked_out_merged_changes(
    changes: tuple[OnTrunkChange, ...],
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
            t"Cannot remove merged {ui.change_id(item.change_id)} because it is "
            t"checked out in {location}.",
            workspaces=workspaces,
        )
