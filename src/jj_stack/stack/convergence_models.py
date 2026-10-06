"""Plans for updating a local stack after GitHub merges or rebases its PRs."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.trunk_evidence import TrunkEvidenceKind


@dataclass(frozen=True, slots=True)
class OnTrunkChange:
    change_id: ChangeId
    candidate: TrackedPR
    evidence_kind: TrunkEvidenceKind
    # The still-open PR to close, or None when GitHub already finished it or rewrote it.
    close_pr: GithubPR | None
    change: LocalCommit | None


@dataclass(frozen=True, slots=True)
class SelectedConvergencePlan:
    on_trunk: tuple[OnTrunkChange, ...]
    remaining_prs: dict[ChangeId, GithubPR]
    remaining_changes: tuple[LocalCommit, ...]
    working_copy_children: tuple[LocalCommit, ...]
    # Where the remaining changes move; None leaves them in place.
    destination: CommitId | None
    # Fetched copies of PR heads that GitHub rewrote and the local commits will replace.
    github_copies: tuple[CommitId, ...]
    # Whether the remaining PRs get the local commits.
    publish: bool
