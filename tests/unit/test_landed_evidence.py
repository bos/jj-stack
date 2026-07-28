from __future__ import annotations

import pytest

from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline
from jj_stack.models.stack import LocalRevision
from jj_stack.review.landed import LandedReviewResult, landed_exit_code
from jj_stack.review.landed_evidence import (
    LandedReviewCandidate,
    classify_exact_snapshot,
    classify_rewritten_result,
    holds_unpublished_edit,
)


def _candidate() -> LandedReviewCandidate:
    return LandedReviewCandidate(
        change_id="change-1",
        review_identity=ReviewIdentity(
            repository_owner="octo-org",
            repository_name="stacked-review",
            pr_number=1,
            head_owner="octo-org",
            head_ref="review/change-1",
        ),
        submitted_baseline=SubmittedBaseline(commit_id="submitted-1"),
    )


def _pull_request(**updates: object) -> GithubPullRequest:
    pull_request = GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(
            label="octo-org:review/change-1",
            ref="review/change-1",
            sha="submitted-1",
        ),
        html_url="https://github.test/octo-org/stacked-review/pull/1",
        merged_at=None,
        number=1,
        state="open",
        title="change 1",
    )
    return pull_request.model_copy(update=updates)


@pytest.mark.landing_recovery
def test_exact_snapshot_evidence_is_identity_and_ancestry_bound() -> None:
    rows = (
        ("on_trunk", _pull_request(), "octo-org", "landed"),
        ("not_on_trunk", _pull_request(), "octo-org", "not_on_trunk"),
        ("unresolved", _pull_request(), "octo-org", "unresolved"),
        (
            "on_trunk",
            _pull_request(head=GithubBranchRef(ref="other", sha="submitted-1")),
            "octo-org",
            "identity_mismatch",
        ),
        ("on_trunk", _pull_request(), "other-org", "identity_mismatch"),
        (
            "on_trunk",
            _pull_request(
                head=GithubBranchRef(
                    label="octo-org:review/change-1",
                    ref="review/change-1",
                    sha="other",
                )
            ),
            "octo-org",
            "head_mismatch",
        ),
    )

    for ancestry, pull_request, owner, expected in rows:
        result = classify_exact_snapshot(
            ancestry=ancestry,
            candidate=_candidate(),
            pull_request=pull_request,
            repository=GithubRepoAddress(
                owner=owner,
                repo="stacked-review",
            ),
        )

        assert result.state == expected


@pytest.mark.landing_recovery
def test_rewritten_result_requires_a_reachable_concrete_merge_result() -> None:
    rows = (
        (_pull_request(), None, "not_merged"),
        (
            _pull_request(state="closed", merged_at="2026-07-21T12:00:00Z"),
            None,
            "merge_result_missing",
        ),
        (
            _pull_request(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "unresolved",
            "merge_result_unresolved",
        ),
        (
            _pull_request(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "not_on_trunk",
            "merge_result_not_on_trunk",
        ),
        (
            _pull_request(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "on_trunk",
            "landed",
        ),
    )

    for pull_request, ancestry, expected in rows:
        result = classify_rewritten_result(
            candidate=_candidate(),
            merge_result_ancestry=ancestry,
            pull_request=pull_request,
            repository=GithubRepoAddress(
                owner="octo-org",
                repo="stacked-review",
            ),
        )

        assert result.state == expected


def test_landed_exit_code_separates_a_deliberate_skip_from_a_failed_write() -> None:
    """Tracking a dependent stack still needs is preserved on purpose, not a failure."""

    candidate = _candidate()
    preserved = LandedReviewResult(
        candidate=candidate,
        outcome="finalized",
        retirement_skip_reason="another local stack still depends on it",
    )
    failed = LandedReviewResult(
        candidate=candidate,
        outcome="finalized",
        retirement_failure="state file is read-only",
    )

    assert landed_exit_code(base=0, results=(preserved,)) == 0
    assert landed_exit_code(base=1, results=(preserved,)) == 1
    assert landed_exit_code(base=0, results=(failed,)) == 1


def _revision(*, commit_id: str, immutable: bool = False) -> LocalRevision:
    return LocalRevision(
        change_id="change-1",
        commit_id=commit_id,
        current_working_copy=False,
        description="feature",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=immutable,
        parents=("parent-1",),
    )


def test_unpublished_edit_check_covers_every_shape_its_callers_pass() -> None:
    """One wrong answer here destroys local work, so pin all four call shapes."""

    published = ("submitted-1",)

    assert not holds_unpublished_edit(published_commit_ids=published, revision=None)
    assert not holds_unpublished_edit(
        published_commit_ids=published,
        revision=_revision(commit_id="submitted-1"),
    )
    assert holds_unpublished_edit(
        published_commit_ids=published,
        revision=_revision(commit_id="edited-locally"),
    )
    # An immutable revision cannot hold a local edit, whatever its commit.
    assert not holds_unpublished_edit(
        published_commit_ids=published,
        revision=_revision(commit_id="edited-locally", immutable=True),
    )
    # Adopting a native survivor also counts the commit GitHub reported for it.
    assert not holds_unpublished_edit(
        published_commit_ids=("submitted-1", "github-rewrote-this"),
        revision=_revision(commit_id="github-rewrote-this"),
    )
