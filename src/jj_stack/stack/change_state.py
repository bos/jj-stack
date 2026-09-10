"""One classifier for a change's relationship to its pull request, PR branch, and trunk.

Every lifecycle command observes the same facts about a tracked change: the saved tracking
pair, the live pull request, the PR branch on the remote, the visible local copies, and whether
the submitted work reached trunk. `classify` turns one `ChangeObservation` into one
`ChangeState`. A `Stop` state carries the one explanation and repair every command shares, so a
command decides only which states it acts on.

`LocalCommit` keeps describing the change itself: conflicts, emptiness, divergence, and working
copies. This module classifies the change's relationship to GitHub, not its local shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from string.templatelib import Interpolation, Template
from typing import TypedDict, overload

import jj_stack.ui as ui
from jj_stack.errors import CliError, DriftCondition, DriftError
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, TrackedPR, TrackingState
from jj_stack.stack.trunk_evidence import (
    CommitAncestry,
    TrunkEvidenceKind,
    classify_trunk_evidence,
)
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class Unobserved:
    """A fact the command did not look up, as opposed to one it observed to be absent."""


UNOBSERVED = Unobserved()


@dataclass(frozen=True, slots=True)
class ObservationFailed:
    """A requested fact could not be observed; it says nothing about presence or absence."""

    error: Message


@dataclass(frozen=True, kw_only=True)
class ChangeObservation:
    """Everything one command observed about one change; unobserved facts stay marked."""

    change_id: ChangeId
    tracked: TrackedPR | None
    # The PR branch the change uses, or would use once submitted.
    branch: str | None
    remote_name: str | None = None
    # Every visible copy of the change; empty when none remains.
    local: tuple[LocalCommit, ...]
    # The copy in the selected stack, when the command selected one.
    selected: LocalCommit | None = None
    # The saved pull request looked up by number; None when GitHub reports none.
    pr: GithubPR | None | Unobserved | ObservationFailed = UNOBSERVED
    open_prs_on_branch: tuple[GithubPR, ...] | Unobserved | ObservationFailed = UNOBSERVED
    # The commit at branch@remote; None when the branch is absent.
    remote_target: CommitId | None | Unobserved = UNOBSERVED
    # Whether PR and ancestry checks found the submitted work on trunk.
    trunk_evidence: TrunkEvidenceKind | None | Unobserved = UNOBSERVED
    trunk_evidence_reason: Message | None = None


@dataclass(frozen=True, kw_only=True)
class TrackedPRObservation(ChangeObservation):
    """A saved PR looked up successfully by number; None means GitHub reports it absent."""

    tracked: TrackedPR
    pr: GithubPR | None = field()
    open_prs_on_branch: tuple[GithubPR, ...] | Unobserved = UNOBSERVED


@dataclass(frozen=True, kw_only=True)
class _State:
    change_id: ChangeId
    tracked: TrackedPR | None
    branch: str | None
    remote_name: str | None
    local: tuple[LocalCommit, ...]
    selected: LocalCommit | None

    @property
    def divergent(self) -> bool:
        """Whether more than one visible commit carries this change."""

        if self.selected is not None and self.selected.divergent:
            return True
        return any(commit.divergent for commit in self.local)

    @property
    def has_local_edits(self) -> bool:
        """Whether the selected local commit differs from the submitted baseline."""

        return (
            self.tracked is not None
            and self.selected is not None
            and self.selected.commit_id != self.tracked.submitted_baseline.commit_id
        )

    def _branch_label(self) -> Message:
        branch = self.branch or "?"
        return ui.bookmark(f"{branch}@{self.remote_name}" if self.remote_name else branch)


@dataclass(frozen=True, kw_only=True)
class WithPR(_State):
    """A state GitHub reported a pull request for."""

    tracked: TrackedPR
    pr: GithubPR
    # The commit at branch@remote, when observed; None when the branch is absent.
    remote_target: CommitId | None | Unobserved
    # Why PR or ancestry checks did not confirm the submitted work on trunk.
    # None when those checks passed or trunk was not inspected.
    trunk_evidence_reason: Message | None = None


class Stop:
    """A state every mutating command stops on; it carries the shared explanation."""

    @property
    def reason(self) -> Message:
        raise NotImplementedError

    @property
    def repair(self) -> Message:
        raise NotImplementedError

    @property
    def drift_condition(self) -> DriftCondition | None:
        """Which cross-system check failed, for callers that report drift by category."""

        return None


# ---- the healthy lifecycle -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Unpublished(_State):
    """No tracking; the change has never been submitted from a tracked repo."""

    # A branch already at the local commit is an interrupted first push, which submit finishes.
    remote_target: CommitId | None | Unobserved


@dataclass(frozen=True, kw_only=True)
class NotInspected(_State):
    """Tracking exists, but this command did not consult GitHub."""


@dataclass(frozen=True, kw_only=True)
class Published(WithPR):
    """The open pull request is at the submitted baseline, and so is the local change."""


@dataclass(frozen=True, kw_only=True)
class Edited(WithPR):
    """The open pull request is at the submitted baseline; the local change moved on."""


@dataclass(frozen=True, kw_only=True)
class PushedUnrecorded(WithPR):
    """The open pull request already follows the local commit, but no baseline records it."""


@dataclass(frozen=True, kw_only=True)
class Queued(WithPR):
    """The open pull request is in the trunk merge queue; every command waits."""


@dataclass(frozen=True, kw_only=True)
class Landed(WithPR):
    """PR and ancestry checks confirm that the submitted work reached trunk."""

    evidence: TrunkEvidenceKind


@dataclass(frozen=True, kw_only=True)
class Merged(WithPR):
    """GitHub reports the PR merged, but checks have not confirmed its work on trunk."""


@dataclass(frozen=True, kw_only=True)
class Closed(WithPR):
    """GitHub reports the pull request closed without merging."""


# ---- stops --------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class LookupFailed(Stop, _State):
    error: Message

    @property
    def reason(self) -> Message:
        return self.error

    @property
    def repair(self) -> Message:
        return t"run {ui.cmd('jj-stack doctor')} to check GitHub access"


@dataclass(frozen=True, kw_only=True)
class PRMissing(Stop, _State):
    tracked: TrackedPR
    open_prs_on_branch: tuple[GithubPR, ...]

    @property
    def drift_condition(self) -> DriftCondition:
        return "saved_pr_missing"

    @property
    def reason(self) -> Message:
        saved_label = format_pr_label(self.tracked.pr_identity.pr_number)
        reason: Message = t"GitHub no longer reports {saved_label}"
        if self.open_prs_on_branch:
            others = ui.join(_pr_label, self.open_prs_on_branch)
            reason = t"{reason}; its PR branch {self._branch_label()} has open {others}"
        return reason

    @property
    def repair(self) -> Message:
        return _RELINK


@dataclass(frozen=True, kw_only=True)
class PRIdentityMismatch(Stop, WithPR):
    @property
    def drift_condition(self) -> DriftCondition:
        return "saved_pr_mismatch"

    @property
    def reason(self) -> Message:
        return (
            t"{_pr_label(self.pr)} now uses head branch {ui.bookmark(self.pr.head.ref)}, not "
            t"the saved PR branch {self._branch_label()}"
        )

    @property
    def repair(self) -> Message:
        return _RELINK


@dataclass(frozen=True, kw_only=True)
class PRAmbiguous(Stop, _State):
    open_prs_on_branch: tuple[GithubPR, ...]

    @property
    def drift_condition(self) -> DriftCondition:
        return "pr_ambiguous"

    @property
    def reason(self) -> Message:
        numbers = ui.join(_pr_label, self.open_prs_on_branch)
        return (
            t"GitHub reports several open pull requests for PR branch "
            t"{self._branch_label()}: {numbers}"
        )

    @property
    def repair(self) -> Message:
        return _RELINK


@dataclass(frozen=True, kw_only=True)
class CompetingOpenPR(Stop, WithPR):
    competitors: tuple[GithubPR, ...]

    @property
    def drift_condition(self) -> DriftCondition:
        return "pr_ambiguous"

    @property
    def ambiguous(self) -> bool:
        """Whether GitHub reports more than one open pull request for the PR branch."""

        return len(self.competitors) + (1 if self.pr.state == "open" else 0) > 1

    @property
    def reason(self) -> Message:
        others = ui.join(_pr_label, self.competitors)
        return (
            t"PR branch {self._branch_label()} also has open {others}; this change is linked "
            t"to {_pr_label(self.pr)}"
        )

    @property
    def repair(self) -> Message:
        others = ui.join(_pr_label, self.competitors)
        return t"close or retarget {others}, or {_RELINK}"


@dataclass(frozen=True, kw_only=True)
class UntrackedPRExists(Stop, _State):
    open_prs_on_branch: tuple[GithubPR, ...]

    @property
    def drift_condition(self) -> DriftCondition:
        return "pr_ambiguous" if len(self.open_prs_on_branch) > 1 else "saved_pr_missing"

    @property
    def reason(self) -> Message:
        prs = ui.join(_pr_label, self.open_prs_on_branch)
        return (
            t"PR branch {self._branch_label()} already has {prs}, but jj-stack has no saved "
            t"pull request link for this change"
        )

    @property
    def repair(self) -> Message:
        return t"choose the intended PR and link it with {ui.cmd('jj-stack relink PR CHANGE')}"


@dataclass(frozen=True, kw_only=True)
class BranchClaimed(Stop, _State):
    remote_target: CommitId

    @property
    def drift_condition(self) -> DriftCondition:
        return "remote_branch_moved"

    @property
    def reason(self) -> Message:
        return (
            t"PR branch {self._branch_label()} already exists at "
            t"commit {ui.commit_id(self.remote_target)}, which does not match this change"
        )

    @property
    def repair(self) -> Message:
        return (
            t"check the branch's work on GitHub before moving or deleting it, or choose another "
            t"PR branch name by editing the subject with "
            t"{ui.cmd(f'jj describe {short_change_id(self.change_id)}')}"
        )


@dataclass(frozen=True, kw_only=True)
class PRHeadMoved(Stop, WithPR):
    @property
    def drift_condition(self) -> DriftCondition:
        return "remote_branch_moved"

    @property
    def reason(self) -> Message:
        head = self.pr.head.sha
        return (
            t"{_pr_label(self.pr)} is at commit {ui.commit_id(head)}, which matches neither this "
            t"change nor its last submitted commit; the PR branch was updated outside this repo"
        )

    @property
    def repair(self) -> Message:
        number = self.pr.number
        short = short_change_id(self.change_id)
        return (
            t"fetch the work with {ui.cmd(f'jj-stack checkout --pull-request {number}')}, or run "
            t"{ui.cmd(f'jj-stack relink --replace-remote {number} {short}')} so the next submit "
            t"replaces it with this change"
        )


@dataclass(frozen=True, kw_only=True)
class BranchMissing(Stop, WithPR):
    @property
    def drift_condition(self) -> DriftCondition:
        return "remote_branch_missing"

    @property
    def reason(self) -> Message:
        return t"PR branch {self._branch_label()} for {_pr_label(self.pr)} no longer exists"

    @property
    def repair(self) -> Message:
        # GitHub closes a pull request whose head branch is deleted and reopens it only once
        # the branch is back, so the two states need different next steps.
        if self.pr.state == "closed":
            return (
                t"restore the branch to reopen {_pr_label(self.pr)}, or run "
                t"{ui.cmd(f'jj-stack cleanup --pull-request {self.pr.number}')} to remove its "
                t"saved link and stack overview comment"
            )
        return (
            t"restore the branch, or close {_pr_label(self.pr)} on GitHub and run "
            t"{ui.cmd(f'jj-stack cleanup --pull-request {self.pr.number}')}"
        )


@dataclass(frozen=True, kw_only=True)
class BranchDisagrees(Stop, WithPR):
    @property
    def drift_condition(self) -> DriftCondition:
        return "remote_branch_moved"

    @property
    def reason(self) -> Message:
        head = self.pr.head.sha
        target = self.remote_target if isinstance(self.remote_target, str) else "?"
        return (
            t"{_pr_label(self.pr)} is at commit {ui.commit_id(head)} but PR branch "
            t"{self._branch_label()} is at commit {ui.commit_id(target)}"
        )

    @property
    def repair(self) -> Message:
        return (
            t"check the PR with {ui.cmd(f'jj-stack view {short_change_id(self.change_id)}')}; "
            t"GitHub may still be catching up with a recent push"
        )


type LinkedPRState = (
    Published
    | Edited
    | PushedUnrecorded
    | Queued
    | Landed
    | Merged
    | Closed
    | PRIdentityMismatch
    | CompetingOpenPR
    | PRHeadMoved
    | BranchMissing
    | BranchDisagrees
)

type TrackedPRState = LinkedPRState | PRMissing | PRAmbiguous
type ChangeState = (
    TrackedPRState | Unpublished | NotInspected | LookupFailed | UntrackedPRExists | BranchClaimed
)

_RELINK: Message = (
    t"check the change with {ui.cmd('jj-stack view CHANGE')}, then link the intended pull "
    t"request with {ui.cmd('jj-stack relink PR CHANGE')}"
)


def _pr_label(pr: GithubPR) -> Message:
    return format_pr_label(pr.number, url=pr.html_url)


# ---- classification ----------------------------------------------------------------------


class _Common(TypedDict):
    change_id: ChangeId
    branch: str | None
    remote_name: str | None
    local: tuple[LocalCommit, ...]
    selected: LocalCommit | None


class _WithPRCommon(_Common):
    tracked: TrackedPR
    pr: GithubPR
    remote_target: CommitId | None | Unobserved
    trunk_evidence_reason: Message | None


@overload
def classify(
    observation: TrackedPRObservation,
    *,
    selected: LocalCommit | None = None,
    ancestries: Mapping[CommitId, CommitAncestry] | None = None,
) -> TrackedPRState: ...


@overload
def classify(
    observation: ChangeObservation,
    *,
    selected: LocalCommit | None = None,
    ancestries: Mapping[CommitId, CommitAncestry] | None = None,
) -> ChangeState: ...


def classify(
    observation: ChangeObservation,
    *,
    selected: LocalCommit | None = None,
    ancestries: Mapping[CommitId, CommitAncestry] | None = None,
) -> ChangeState:
    """Derive one state from one observation; unobserved facts never produce a stop."""

    o = observation
    if selected is not None:
        o = replace(o, selected=selected)
    if ancestries is not None and o.tracked is not None and isinstance(o.pr, GithubPR):
        evidence, reason = classify_trunk_evidence(
            ancestries=ancestries, candidate=o.tracked, pr=o.pr
        )
        o = replace(o, trunk_evidence=evidence, trunk_evidence_reason=reason)
    common = _Common(
        change_id=o.change_id,
        branch=o.branch,
        remote_name=o.remote_name,
        local=o.local,
        selected=o.selected,
    )
    if isinstance(o.pr, ObservationFailed):
        return LookupFailed(**common, tracked=o.tracked, error=o.pr.error)
    if isinstance(o.open_prs_on_branch, ObservationFailed):
        return LookupFailed(**common, tracked=o.tracked, error=o.open_prs_on_branch.error)
    open_prs = () if isinstance(o.open_prs_on_branch, Unobserved) else o.open_prs_on_branch
    if o.tracked is None:
        return _classify_untracked(o, common, open_prs)
    if isinstance(o.pr, Unobserved):
        return NotInspected(**common, tracked=o.tracked)
    if o.pr is None:
        if len(open_prs) > 1:
            return PRAmbiguous(**common, tracked=o.tracked, open_prs_on_branch=open_prs)
        return PRMissing(**common, tracked=o.tracked, open_prs_on_branch=open_prs)
    return _classify_pr(o, common, open_prs, o.pr, o.tracked)


def _classify_pr(
    o: ChangeObservation,
    common: _Common,
    open_prs: tuple[GithubPR, ...],
    pr: GithubPR,
    tracked: TrackedPR,
) -> LinkedPRState:
    if pr.head.ref != tracked.pr_identity.head_ref:
        return PRIdentityMismatch(**common, tracked=tracked, pr=pr, remote_target=o.remote_target)
    evidence = o.trunk_evidence
    with_pr = _WithPRCommon(
        **common,
        tracked=tracked,
        pr=pr,
        remote_target=o.remote_target,
        trunk_evidence_reason=(
            o.trunk_evidence_reason
            if evidence is None and not isinstance(evidence, Unobserved)
            else None
        ),
    )
    competitors = tuple(candidate for candidate in open_prs if candidate.number != pr.number)
    if competitors:
        return CompetingOpenPR(**with_pr, competitors=competitors)
    if isinstance(evidence, str):
        return Landed(**with_pr, evidence=evidence)
    if pr.state == "merged":
        return Merged(**with_pr)
    # Deleting a pull request's head branch also closes the PR. Report the missing branch first:
    # it is the external drift that made the saved identity unusable and names the repair that
    # can preserve the PR. A merged PR remains merge evidence even when GitHub deleted its branch.
    if o.remote_target is None:
        return BranchMissing(**with_pr)
    if pr.state == "closed":
        return Closed(**with_pr)
    return _classify_open(o, with_pr, pr)


def _classify_untracked(
    o: ChangeObservation,
    common: _Common,
    open_prs: tuple[GithubPR, ...],
) -> ChangeState:
    if open_prs:
        return UntrackedPRExists(**common, tracked=None, open_prs_on_branch=open_prs)
    remote = o.remote_target
    if isinstance(remote, str) and remote not in _local_commit_ids(o):
        return BranchClaimed(**common, tracked=None, remote_target=remote)
    return Unpublished(**common, tracked=None, remote_target=remote)


def _classify_open(
    o: ChangeObservation,
    with_pr: _WithPRCommon,
    pr: GithubPR,
) -> LinkedPRState:
    baseline = with_pr["tracked"].submitted_baseline.commit_id
    head = pr.head.sha
    remote = o.remote_target
    if head != baseline and head not in _local_commit_ids(o):
        return PRHeadMoved(**with_pr)
    if not isinstance(remote, Unobserved) and remote != head:
        return BranchDisagrees(**with_pr)
    # A queued pull request whose head and branch still agree with what was submitted waits
    # for GitHub; one that no longer agrees is reported as moved first.
    if pr.is_queued:
        return Queued(**with_pr)
    if head != baseline:
        return PushedUnrecorded(**with_pr)
    local_commit = _selected_commit_id(o)
    if local_commit is not None and local_commit != baseline:
        return Edited(**with_pr)
    return Published(**with_pr)


def _selected_commit_id(o: ChangeObservation) -> CommitId | None:
    if o.selected is not None:
        return o.selected.commit_id
    if len(o.local) == 1:
        return o.local[0].commit_id
    return None


def _local_commit_ids(o: ChangeObservation) -> frozenset[CommitId]:
    ids = {commit.commit_id for commit in o.local}
    if o.selected is not None:
        ids.add(o.selected.commit_id)
    return frozenset(ids)


def live_pr(state: ChangeState) -> GithubPR | None:
    """The pull request GitHub reported for this state, if any."""

    return state.pr if isinstance(state, WithPR) else None


def trunk_evidence_reason(state: WithPR) -> Message:
    """Explain why the PR and ancestry checks did not confirm that its work reached trunk."""

    if state.trunk_evidence_reason is not None:
        return state.trunk_evidence_reason
    if isinstance(state, Stop):
        return state.reason
    return "no merge result is on trunk"


def stop_error(state: Stop, *, rerun: str) -> CliError:
    """Fail closed on one stop state with its shared wording and the command to rerun."""

    message: Message = t"{state.reason}."
    hint: Message = t"{_capitalized(state.repair)}, then rerun {ui.cmd(rerun)}."
    condition = state.drift_condition
    if condition is None:
        return CliError(message, hint=hint)
    return DriftError(message, condition=condition, hint=hint)


def _capitalized(message: Message) -> Message:
    if isinstance(message, str):
        return message[:1].upper() + message[1:]
    if isinstance(message, tuple):
        return (_capitalized(message[0]), *message[1:]) if message else message
    if isinstance(message, Template):
        first = message.strings[0]
        interpolations = list(message.interpolations)
        if first:
            first = first[:1].upper() + first[1:]
        elif interpolations:
            # The message starts with another message, such as a shared repair clause.
            leading = interpolations[0]
            interpolations[0] = Interpolation(
                _capitalized(leading.value),
                leading.expression,
                leading.conversion,
                leading.format_spec,
            )
        parts: list[str | Interpolation] = [first]
        for interpolation, text in zip(interpolations, message.strings[1:], strict=True):
            parts.extend((interpolation, text))
        return Template(*parts)
    return message


# ---- shared derived rules ---------------------------------------------------------------


def report_incomplete(state: ChangeState) -> bool:
    """Whether this change stops `view` and `list` from reporting the stack completely.

    Both report commands share this rule so the same repo cannot yield a complete report from
    one and an incomplete report from the other. A saved pull request whose GitHub state went
    unobserved counts the same way a failed lookup does. Divergence of merged work is history
    exposed by a fetch, not an incomplete report.
    """

    if state.divergent and not isinstance(state, (Landed, Merged)):
        return True
    if isinstance(state, CompetingOpenPR):
        return state.ambiguous
    return isinstance(state, (LookupFailed, NotInspected, PRAmbiguous, PRMissing))


@dataclass(frozen=True, slots=True)
class OrphanedRecord:
    """A saved tracking record whose change has left every live stack."""

    change_id: ChangeId
    pr_identity: PRIdentity


def enumerate_orphaned_records(
    state: TrackingState,
    local_stacks: Sequence[LocalStack],
) -> tuple[OrphanedRecord, ...]:
    """Return saved PR records whose change is no longer in any live stack."""

    live_change_ids = {change.change_id for stack in local_stacks for change in stack.changes}
    return tuple(
        OrphanedRecord(change_id=change_id, pr_identity=tracked.pr_identity)
        for change_id, tracked in state.prs.items()
        if change_id not in live_change_ids
    )
