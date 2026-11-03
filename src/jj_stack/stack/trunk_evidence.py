"""Pure classification of whether a tracked pull request's work is on trunk.

Check whether the submitted commit or GitHub's rewritten merge result is an ancestor of trunk.
The first check requires the PR head to still be the submitted commit; the second also accepts
GitHub's rewrite of it with the same content. A PR's merged state alone does not show that its
work reached this repo's trunk.
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
    head_rewritten: bool,
    pr: GithubPR,
) -> tuple[TrunkEvidenceKind | None, Message]:
    """Return the kind of evidence that the work reached trunk, or why neither check passed.

    `head_rewritten` says the PR head is GitHub's rewrite of the submitted commit with the same
    content. Merging a GitHub stack one PR at a time merges each later PR from such a rewrite.
    """

    pr_label = format_pr_label(pr.number, url=pr.html_url)
    submitted = candidate.submitted_baseline.commit_id
    head_submitted = pr.head.sha == submitted
    if not head_submitted and not head_rewritten:
        return None, t"{pr_label} no longer points to the last submitted commit"
    if head_submitted and ancestries[submitted] == "on_trunk":
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
