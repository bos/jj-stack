from __future__ import annotations

import pytest

from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.stack.trunk_evidence import CommitAncestry, classify_trunk_evidence
from tests.support.change_helpers import make_change


def _candidate() -> TrackedPR:
    return TrackedPR(
        pr_identity=PRIdentity(pr_number=1, head_ref="jj-stack/change-1"),
        submitted_baseline=SubmittedBaseline(commit_id="submitted-1"),
    )


def _pr(**updates: object) -> GithubPR:
    pr = GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubPRHead(
            label="octo-org:jj-stack/change-1",
            ref="jj-stack/change-1",
            sha="submitted-1",
        ),
        html_url="https://github.test/octo-org/stacked-prs/pull/1",
        node_id="PR_1",
        number=1,
        state="open",
        title="change 1",
    )
    return pr.model_copy(update=updates)


def _moved_head() -> GithubPR:
    return _pr(
        head=GithubPRHead(label="octo-org:jj-stack/change-1", ref="jj-stack/change-1", sha="x")
    )


@pytest.mark.merge_recovery
def test_trunk_evidence_needs_the_pr_head_at_the_submitted_commit_and_a_result_on_trunk() -> None:
    merged = _pr(state="merged", merge_commit_sha="merge-1")
    rows: tuple[tuple[GithubPR, CommitAncestry, CommitAncestry | None, str | None], ...] = (
        (_pr(), "on_trunk", None, "exact"),
        (_pr(), "not_on_trunk", None, None),
        (_pr(), "unresolved", None, None),
        (_moved_head(), "on_trunk", None, None),
        (_pr(state="merged"), "not_on_trunk", None, None),
        (merged, "not_on_trunk", "unresolved", None),
        (merged, "not_on_trunk", "not_on_trunk", None),
        (merged, "not_on_trunk", "on_trunk", "rewritten"),
    )

    for pr, submitted_ancestry, merge_ancestry, expected in rows:
        ancestries: dict[str, CommitAncestry] = {"submitted-1": submitted_ancestry}
        if merge_ancestry is not None:
            ancestries["merge-1"] = merge_ancestry
        kind, reason = classify_trunk_evidence(
            ancestries=ancestries, candidate=_candidate(), pr=pr
        )

        assert kind == expected
        # An unsuccessful check includes a reason for the caller to report.
        assert (kind is not None) or reason


def _change(*, commit_id: str, empty: bool = False, immutable: bool = False) -> LocalCommit:
    return make_change(
        change_id="change-1",
        commit_id=commit_id,
        description="feature",
        empty=empty,
        immutable=immutable,
    )


def test_unpublished_edit_check_covers_every_shape_its_callers_pass() -> None:
    """One wrong answer here destroys local work, so pin every shape callers pass."""

    submitted = "submitted-1"

    assert not _change(commit_id="submitted-1").holds_unpublished_edit(submitted)
    assert _change(commit_id="edited-locally").holds_unpublished_edit(submitted)
    # An immutable change cannot hold a local edit, whatever its commit.
    assert not _change(commit_id="edited-locally", immutable=True).holds_unpublished_edit(
        submitted
    )
    # An empty change modifies no files relative to its parent, so a rewrite that emptied it is
    # safe to remove.
    assert not _change(commit_id="rebased-onto-trunk", empty=True).holds_unpublished_edit(
        submitted
    )
