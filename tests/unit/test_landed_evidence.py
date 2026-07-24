from __future__ import annotations

import pytest

from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline
from jj_stack.review.landed_evidence import (
    LandedReviewCandidate,
    classify_exact_snapshot,
    classify_rewritten_result,
)


def _candidate() -> LandedReviewCandidate:
    return LandedReviewCandidate(
        change_id="change-1",
        review_identity=ReviewIdentity(
            github_host="github.test",
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
                host="github.test",
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
                host="github.test",
                owner="octo-org",
                repo="stacked-review",
            ),
        )

        assert result.state == expected
