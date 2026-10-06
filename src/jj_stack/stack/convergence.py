"""Plan how sync updates a local stack after GitHub merges or rebases its pull requests."""

from __future__ import annotations

from dataclasses import replace

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.change_state import (
    Closed,
    Landed,
    Merged,
    Rewritten,
    Stop,
    WithPR,
    classify,
    stop_error,
    trunk_evidence_reason,
)
from jj_stack.stack.convergence_models import OnTrunkChange, SelectedConvergencePlan
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.github_stack_safety import selected_github_stack
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.stack.preparation import PreparedLocalStack
from jj_stack.stack.trunk_evidence import CommitAncestry


class CheckedOutMergedChangeError(CliError):
    def __init__(self, message: ui.Message, *, workspaces: tuple[str, ...]) -> None:
        super().__init__(message)
        self.workspaces = workspaces


def build_selected_convergence_plan(
    *,
    ancestries: dict[CommitId, CommitAncestry],
    github_stacks: tuple[GithubStack, ...],
    head_children: tuple[LocalCommit, ...],
    observation: RepoFacts,
    prepared: PreparedLocalStack,
) -> SelectedConvergencePlan:
    """Decide every stop before anything changes, then where the remaining changes go."""

    selected = prepared.stack.changes
    state = prepared.state
    head = short_change_id(selected[-1].change_id)
    rerun = f"jj-stack sync {head}"
    history = _github_stack_history(
        ancestries=ancestries,
        github_stacks=github_stacks,
        observation=observation,
        prepared=prepared,
        rerun=rerun,
    )
    history_ids = {item.change_id for item in history}
    on_trunk = list(history)
    remaining_changes: list[LocalCommit] = []
    surviving: dict[ChangeId, WithPR] = {}
    for change in (item for item in selected if item.change_id not in history_ids):
        candidate = state.prs.get(change.change_id)
        if candidate is None:
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
                hint=_trunk_evidence_hint(change_state, rerun=rerun),
            )
        if not isinstance(change_state, Landed):
            remaining_changes.append(change)
            surviving[change.change_id] = change_state
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
        pr = change_state.pr
        on_trunk.append(
            OnTrunkChange(
                change_id=change.change_id,
                candidate=candidate,
                evidence_kind=evidence_kind,
                close_pr=pr if evidence_kind == "exact" and pr.state == "open" else None,
                change=change,
            )
        )

    _require_no_unpublished_edits(tuple(on_trunk), head=head)
    _require_no_checked_out_merged_changes(tuple(on_trunk))
    submitted = _remaining_submitted_prs(
        remaining_changes=tuple(remaining_changes), prs=surviving, head=head
    )
    local_head = selected[-1]
    working_copy_children = tuple(
        commit
        for commit in head_children
        if commit.is_working_copy
        and not commit.has_described_work
        and commit.parents == (local_head.commit_id,)
    )
    states = tuple(submitted.values())
    # A fetch of the PR branches shows GitHub's rewrites as extra copies of the changes.
    github_copies = {
        copy.commit_id
        for item in states
        if isinstance(item, Rewritten)
        for copy in observation.prs[item.change_id].local
        if copy.commit_id == item.pr.head.sha
    }
    _require_rebasable(
        (*remaining_changes, *working_copy_children),
        github_copies=github_copies,
        observation=observation,
        head=head,
    )
    destination = None
    if on_trunk:
        destination = prepared.stack.trunk.commit_id
    elif (
        states
        and isinstance(rewritten := states[0], Rewritten)
        and not prepared.client.on_first_parent_chain(
            rewritten.rewrite_parent, tip=remaining_changes[0].parents[0]
        )
    ):
        # GitHub rebased the stack without merging any of it, onto a trunk commit it chose.
        # A stack already at or past that commit stays where it is.
        destination = rewritten.rewrite_parent
    return SelectedConvergencePlan(
        on_trunk=tuple(on_trunk),
        remaining_prs={change_id: item.pr for change_id, item in submitted.items()},
        remaining_changes=tuple(remaining_changes),
        working_copy_children=working_copy_children,
        destination=destination,
        github_copies=tuple(github_copies),
        publish=bool(on_trunk)
        or any(item.pr.head.sha != item.tracked.submitted_baseline.commit_id for item in states),
    )


def _remaining_submitted_prs(
    *,
    remaining_changes: tuple[LocalCommit, ...],
    prs: dict[ChangeId, WithPR],
    head: str,
) -> dict[ChangeId, WithPR]:
    """Return the remaining submitted PRs; unsubmitted changes must come after them."""

    submitted: dict[ChangeId, WithPR] = {}
    saw_unsubmitted = False
    for change in remaining_changes:
        if (pr := prs.get(change.change_id)) is None:
            saw_unsubmitted = True
            continue
        if saw_unsubmitted:
            raise CliError(
                t"Cannot sync because submitted {ui.change_id(change.change_id)} appears "
                t"above an unsubmitted change.",
                hint=t"Submit the complete stack with {ui.cmd(f'jj-stack submit {head}')}, or "
                t"select a stack that ends below the unsubmitted change.",
            )
        submitted[change.change_id] = pr
    return submitted


def _member_state(
    *,
    ancestries: dict[CommitId, CommitAncestry],
    change_id: ChangeId,
    observation: RepoFacts,
    rerun: str,
    selected: LocalCommit | None,
) -> WithPR:
    """Classify one tracked change with its trunk evidence, stopping on a broken saved link."""

    observed = observation.prs[change_id]
    state = classify(replace(observed, selected=selected), ancestries=ancestries)
    if isinstance(state, Stop):
        raise stop_error(state, rerun=rerun)
    return state


def _closed_error(state: Closed) -> CliError:
    pr_label = format_pr_label(state.pr.number, url=state.pr.html_url)
    return CliError(
        t"{pr_label} for {ui.change_id(state.change_id)} is closed, so jj-stack cannot update "
        t"that PR.",
        hint=t"Reopen it on GitHub, or run "
        t"{ui.cmd(f'jj-stack cleanup --pull-request {state.pr.number}')} before the next submit.",
    )


def _require_rebasable(
    changes: tuple[LocalCommit, ...],
    *,
    github_copies: set[CommitId],
    observation: RepoFacts,
    head: str,
) -> None:
    for change in changes:
        observed = observation.prs.get(change.change_id)
        divergent = (
            change.divergent
            if observed is None
            else any(
                copy.commit_id not in github_copies and copy.commit_id != change.commit_id
                for copy in observed.local
            )
        )
        if divergent:
            raise CliError(
                t"Cannot rebase remaining {ui.change_id(change.change_id)} because it has "
                t"more than one local copy.",
                hint=divergence_recovery_hint(
                    change.change_id,
                    retry=t"rerun {ui.cmd(f'jj-stack sync {head}')} for this stack",
                ),
            )
        if change.immutable:
            # Typically trunk already holds it while GitHub has not yet reported the merge.
            raise CliError(
                t"Cannot rebase remaining {ui.change_id(change.change_id)} because it is "
                t"immutable.",
                hint=t"Check it with {ui.cmd(f'jj-stack view {head}')}. Once GitHub reports "
                t"its pull request merged, rerun {ui.cmd(f'jj-stack sync {head}')}.",
            )


def _github_stack_history(
    *,
    ancestries: dict[CommitId, CommitAncestry],
    github_stacks: tuple[GithubStack, ...],
    observation: RepoFacts,
    prepared: PreparedLocalStack,
    rerun: str,
) -> tuple[OnTrunkChange, ...]:
    """The merged members of the selection's GitHub stack, including ones with no local copy."""

    selected = prepared.stack.changes
    state = prepared.state
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
        return ()
    selected_by_id = {change.change_id: change for change in selected}
    history: list[OnTrunkChange] = []
    for member in stack.prs:
        change_id = by_pr.get(member.number)
        if change_id is None:
            continue
        if not member.is_historical:
            pr = observation.prs[change_id].pr
            if pr is not None and pr.state == "merged":
                raise CliError(
                    t"PR #{pr.number} is merged, but GitHub stack #{stack.number} still lists "
                    t"it as active.",
                    hint=t"Wait for GitHub to update the stack, then rerun {ui.cmd(rerun)}.",
                )
            continue
        member_state = _member_state(
            change_id=change_id,
            ancestries=ancestries,
            observation=observation,
            rerun=rerun,
            selected=selected_by_id.get(change_id),
        )
        history.append(
            _historical_member(
                candidate=state.prs[change_id],
                change_id=change_id,
                head=short_change_id(selected[-1].change_id),
                member_state=member_state,
                observation=observation,
                selected=selected_by_id.get(change_id),
            )
        )
    return tuple(history)


def _historical_member(
    *,
    candidate: TrackedPR,
    change_id: ChangeId,
    head: str,
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
                retry=t"rerun {ui.cmd(f'jj-stack sync {head}')}",
            ),
        )
    if not isinstance(member_state, Landed):
        pr_label = format_pr_label(member_state.pr.number, url=member_state.pr.html_url)
        raise CliError(
            t"Cannot remove the saved link for merged {pr_label}: "
            t"{trunk_evidence_reason(member_state)}.",
            hint=_trunk_evidence_hint(member_state, rerun=f"jj-stack sync {head}"),
        )
    return OnTrunkChange(
        change_id,
        candidate,
        member_state.evidence,
        None,
        selected or (mutable_copies[0] if mutable_copies else None),
    )


def _trunk_evidence_hint(state: WithPR, *, rerun: str) -> ui.Message:
    """Say how to resolve a merged PR whose work jj-stack cannot place on trunk."""

    if state.pr.head.sha == state.tracked.submitted_baseline.commit_id:
        return (
            t"Check that {ui.revset('trunk()')} selects the branch the PR merged into, then "
            t"rerun {ui.cmd(rerun)}."
        )
    pr_label = format_pr_label(state.pr.number, url=state.pr.html_url)
    short = short_change_id(state.change_id)
    return (
        t"{pr_label} merged from commit {ui.commit_id(state.pr.head.sha)}, not from the commit "
        t"jj-stack pushed, so jj-stack cannot tell whether this change is part of what merged. "
        t"Check the files {pr_label} changed on GitHub against {ui.cmd(f'jj diff -r {short}')}. "
        t"If this change is in them, run {ui.cmd(f'jj abandon {short}')} and then "
        t"{ui.cmd('jj-stack cleanup')}; if not, run "
        t"{ui.cmd(f'jj-stack unstack --local {short}')} and submit again."
    )


def _require_no_unpublished_edits(changes: tuple[OnTrunkChange, ...], *, head: str) -> None:
    for item in changes:
        local, baseline = item.change, item.candidate.submitted_baseline.commit_id
        if local is None or not local.holds_unpublished_edit(baseline):
            continue
        short = short_change_id(local.change_id)
        raise CliError(
            t"Cannot remove merged {ui.change_id(item.change_id)}: its local commit changed "
            t"since submit and is not empty. Removing it could discard local work.",
            hint=t"Run {ui.cmd(f"jj rebase -s {short} -o 'trunk()'")} and rerun "
            t"{ui.cmd(f'jj-stack sync {head}')}. If "
            t"{ui.cmd(f'jj diff -r {short}')} still shows changes, move anything still needed "
            t"to another change, then drop this copy with {ui.cmd(f'jj abandon {short}')} and "
            t"rerun {ui.cmd(f'jj-stack sync {head}')}, or keep it and forget the selected "
            t"stack's saved links with "
            t"{ui.cmd(f'jj-stack unstack --local {short}')}.",
        )


def _require_no_checked_out_merged_changes(
    changes: tuple[OnTrunkChange, ...],
) -> None:
    for item in changes:
        change = item.change
        # Only another workspace blocks removal, because abandoning its working copy would leave
        # it stale. The current workspace moves to trunk before the change is abandoned. jj lists
        # working copies by name only when there are several workspaces, including the current.
        if change is None or len(change.working_copy_workspaces) <= int(
            change.current_working_copy
        ):
            continue
        workspaces = change.working_copy_workspaces
        if len(workspaces) == 1:
            location = t"workspace {ui.code(workspaces[0])}"
        else:
            location = t"workspaces {ui.join(ui.code, workspaces)}"
        raise CheckedOutMergedChangeError(
            t"Cannot remove merged {ui.change_id(item.change_id)} because it is "
            t"checked out in {location}.",
            workspaces=workspaces,
        )
