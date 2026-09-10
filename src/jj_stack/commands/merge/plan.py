"""Plan a stack's merge and define the records the merge command and its executor share."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.formatting import format_pr_number
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import PRIdentity, TrackingState
from jj_stack.stack.change_state import (
    BranchDisagrees,
    BranchMissing,
    PRAmbiguous,
    PRHeadMoved,
    PRIdentityMismatch,
    PRMissing,
    TrackedPRObservation,
    classify,
)
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class MergeAction:
    """One planned, applied, or blocked merge action."""

    kind: str
    body: Message
    status: Literal["applied", "blocked", "planned"]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Rendered merge result for one selected local stack."""

    actions: tuple[MergeAction, ...]
    enqueued: bool
    trunk_branch: str
    trunk_subject: str
    final_trunk_commit_id: str | None = None

    @property
    def applied(self) -> bool:
        return any(action.status == "applied" for action in self.actions)

    @property
    def blocked(self) -> bool:
        return any(action.status == "blocked" for action in self.actions)


@dataclass(frozen=True, slots=True)
class MergeExecutionInputs:
    """Mutation dependencies independent of normal stack/status preparation."""

    repo: GithubRepoAddress
    selected_revset: str
    trunk_branch: str
    trunk_subject: str

    def result(
        self,
        *,
        actions: tuple[MergeAction, ...],
        enqueued: bool = False,
        final_trunk_commit_id: str | None = None,
    ) -> MergeResult:
        return MergeResult(
            actions=actions,
            enqueued=enqueued,
            final_trunk_commit_id=final_trunk_commit_id,
            trunk_branch=self.trunk_branch,
            trunk_subject=self.trunk_subject,
        )


@dataclass(frozen=True, slots=True)
class MergeChange:
    """One selected change plus its GitHub link."""

    base_ref: str
    change_id: ChangeId
    commit_id: CommitId
    identity: PRIdentity


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Resolved merge plan for the selected stack."""

    boundary_action: MergeAction | None
    planned_changes: tuple[MergeChange, ...]
    linked_changes: tuple[MergeChange, ...]


def build_merge_plan(
    *,
    observation: RepoFacts,
    remote_name: str,
    repo: GithubRepoAddress,
    changes: tuple[LocalCommit, ...],
    state: TrackingState,
    target_change_id: str | None,
    trunk_branch: str,
) -> MergePlan:
    merge_changes = tuple(_merge_change(observation, change, state) for change in changes)
    candidates: list[MergeChange] = []
    boundary: Message | None = None
    for local, change in zip(changes, merge_changes, strict=True):
        if change is None:
            boundary = _boundary(
                local,
                t"it has no usable pull request link; run {ui.cmd('jj-stack submit')} "
                t"for new work, or {ui.cmd('jj-stack relink <pr> <change-id>')} "
                t"to repair an existing link",
            )
            break
        error = merge_precondition_error(
            expected_repo=repo,
            expected_trunk_branch=trunk_branch,
            observation=observation,
            remote_name=remote_name,
            change=change,
            sync_target=short_change_id(changes[-1].change_id),
        )
        if error is not None:
            boundary = _boundary(local, error)
            break
        candidates.append(change)
    if target_change_id is not None:
        target = next(
            (
                index + 1
                for index, change in enumerate(candidates)
                if change.change_id == target_change_id
            ),
            0,
        )
        candidates = candidates[:target]
        boundary = (
            None
            if target
            else boundary
            or (
                "the selected PR is above the PRs that can merge right now; run "
                "jj-stack view to see which PRs at the bottom of the stack are ready"
            )
        )
    action = (
        MergeAction(
            kind="boundary",
            body=boundary or "No PRs on the selected stack can be merged.",
            status="blocked" if not candidates else "planned",
        )
        if boundary is not None or not candidates
        else None
    )
    return MergePlan(
        boundary_action=action,
        planned_changes=tuple(candidates),
        linked_changes=tuple(change for change in merge_changes if change is not None),
    )


def merge_precondition_error(
    *,
    expected_repo: GithubRepoAddress,
    expected_trunk_branch: str,
    observation: RepoFacts,
    remote_name: str,
    change: MergeChange,
    sync_target: str,
) -> Message | None:
    """Explain which observed precondition prevents the next action and how to clear it."""

    remote = observation.remote
    if remote is None or remote.name != remote_name:
        return _inspect(f"Git remote {remote_name} is no longer configured")
    if observation.configured_repo != expected_repo:
        return _inspect("the configured Git remote no longer names the planned GitHub repo")
    github_repo = observation.github_repo
    if github_repo.full_name.casefold() != expected_repo.full_name.casefold():
        return _inspect("GitHub no longer reports the planned repo")
    if github_repo.default_branch not in (None, "", expected_trunk_branch):
        return _inspect("GitHub no longer reports the planned trunk branch as its default")
    return _merge_change_precondition_error(
        observation.prs[change.change_id], change, sync_target
    )


def _merge_change_precondition_error(
    observed: TrackedPRObservation, change: MergeChange, sync_target: str
) -> Message | None:
    # GitHub's report of the pull request comes first: a merged pull request is a stop by itself,
    # wherever its branch and the local copy have ended up since. Only a candidate that can still
    # merge goes on to the commit comparison.
    selected = next(
        (commit for commit in observed.local if commit.commit_id == change.commit_id),
        None,
    )
    state = classify(observed, selected=selected)
    if isinstance(state, (PRMissing, PRAmbiguous, PRIdentityMismatch)):
        return t"{state.reason}; {state.repair}"
    pr = state.pr
    pr_number = format_pr_number(pr.number, url=pr.html_url)
    if pr.state == "merged":
        return (
            t"pull request {pr_number} is merged; update the local stack with "
            t"{ui.cmd(f'jj-stack sync {sync_target}')}"
        )
    if pr.state != "open":
        return _inspect(t"pull request {pr_number} is {pr.state}")
    if pr.is_draft:
        return _inspect(t"pull request {pr_number} is now a draft")
    submit = ui.cmd(f"jj-stack submit {short_change_id(change.change_id)}")
    if not observed.local:
        return (
            t"it is no longer visible locally; find where it went with {ui.cmd('jj-stack view')}"
        )
    if state.divergent:
        hint = divergence_recovery_hint(change.change_id, retry=t"run {submit}")
        return t"it has more than one local version; {hint}"
    # Conflicts come before the commit comparison: a rebase that conflicts also changes the
    # commit, and resolving is what has to happen first either way.
    if observed.local[0].conflict:
        return t"it has unresolved conflicts; resolve them with jj, then run {submit}"
    if isinstance(state, (PRHeadMoved, BranchMissing, BranchDisagrees)):
        return t"{state.reason}; {state.repair}"
    # Merge only the exact submitted commit: the planned commit must be the local commit, the
    # submitted baseline, and the PR branch target alike.
    if selected is None or state.has_local_edits or state.remote_target != change.commit_id:
        return (
            t"the local change or its PR branch no longer matches the last submitted commit; "
            t"run {submit}"
        )
    return None


def _inspect(reason: Message) -> Message:
    return t"{reason}; inspect it and rerun {ui.cmd('jj-stack merge')}"


def _merge_change(
    observation: RepoFacts,
    change: LocalCommit,
    state: TrackingState,
) -> MergeChange | None:
    candidate = state.prs.get(change.change_id)
    observed = observation.prs.get(change.change_id)
    if candidate is None or observed is None or (pr := observed.pr) is None:
        return None
    return MergeChange(
        base_ref=pr.base.ref,
        change_id=change.change_id,
        commit_id=change.commit_id,
        identity=candidate.pr_identity,
    )


def _boundary(change: LocalCommit, reason: Message) -> Message:
    return (
        t"before {change.subject} {ui.change_id(change.change_id)} because ",
        reason,
    )
