"""Pure classification of whether a tracked pull request's work is on trunk.

Check whether the submitted commit or GitHub's rewritten merge result is an ancestor of trunk.
Both checks first require the PR head to still be the submitted commit. A PR's merged state alone
does not show that its work reached this repo's trunk.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import jj_stack.ui as ui
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import CommitId
from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import TrackedPR
from jj_stack.ui import Message

CommitAncestry = Literal["not_on_trunk", "on_trunk", "unresolved"]
TrunkEvidenceKind = Literal["exact", "rewritten"]


def classify_trunk_evidence(
    *,
    ancestries: Mapping[CommitId, CommitAncestry],
    candidate: TrackedPR,
    pr: GithubPR,
) -> tuple[TrunkEvidenceKind | None, Message]:
    """Return the kind of evidence that the work reached trunk, or why neither check passed."""

    pr_label = format_pr_label(pr.number, url=pr.html_url)
    submitted = candidate.submitted_baseline.commit_id
    if pr.head.sha != submitted:
        return None, t"{pr_label} no longer points to the last submitted commit"
    ancestry = ancestries[submitted]
    if ancestry == "on_trunk":
        return "exact", ""
    if pr.state != "merged":
        return None, t"{pr_label} is {pr.state} without a result on trunk"
    merge_commit_id = pr.merge_commit_sha
    if merge_commit_id is None:
        return None, t"GitHub did not report the commit produced by merging {pr_label}"
    merge_ancestry = ancestries.get(merge_commit_id)
    if merge_ancestry == "on_trunk":
        return "rewritten", ""
    if merge_ancestry == "unresolved":
        return None, (
            t"commit {ui.commit_id(merge_commit_id)} from GitHub's merge is unavailable locally"
        )
    return None, t"commit {ui.commit_id(merge_commit_id)} from GitHub's merge is not on trunk"
