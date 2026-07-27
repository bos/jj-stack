from __future__ import annotations

from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.review.change_status import (
    classify_review_change,
)
from jj_stack.review.status import PullRequestLookup


def _pull_request(*, draft: bool = False, state: str = "open") -> GithubPullRequest:
    merged_at = "2026-05-09T12:00:00Z" if state == "merged" else None
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        draft=draft,
        head=GithubBranchRef(ref="review/change"),
        html_url="https://github.test/octo/repo/pull/1",
        merged_at=merged_at,
        number=1,
        state="closed" if state == "merged" else state,
        title="change",
    ).normalize_state()


def _identity(*, pr_number: int = 1) -> ReviewIdentity:
    return ReviewIdentity(
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref="review/change",
    )


def test_classifier_keeps_draft_and_review_decision_as_separate_axes() -> None:
    status = classify_review_change(
        commit_id="commit-1",
        local="present",
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=_pull_request(draft=True),
            review_decision="approved",
            review_decision_error=None,
            state="open",
        ),
        remote_target="commit-1",
        review_identity=_identity(),
    )

    assert status.pr_lifecycle == "open"
    assert status.pr_draft is True
    assert status.pr_review_decision == "approved"


def test_classifier_marks_missing_lookup_with_saved_pr_identity_as_stale_link() -> None:
    status = classify_review_change(
        commit_id="commit-1",
        local="present",
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=None,
            review_decision=None,
            review_decision_error=None,
            state="missing",
        ),
        remote_target=None,
        review_identity=_identity(),
    )

    assert status.pr_lifecycle == "missing"
    assert status.has_stale_pull_request_link is True


def test_classifier_reports_saved_review_identity() -> None:
    status = classify_review_change(
        commit_id="commit-1",
        local="present",
        pull_request_lookup=None,
        remote_target=None,
        review_identity=_identity(),
    )

    assert status.saved_review_identity is True


def test_classifier_marks_direct_remote_target_current() -> None:
    current_status = classify_review_change(
        commit_id="commit-1",
        local="present",
        pull_request_lookup=None,
        remote_target="commit-1",
    )

    assert current_status.remote_branch == "current"
    assert current_status.remote_branch_matches_commit is True


def test_classifier_marks_direct_remote_target_that_does_not_match_commit() -> None:
    status = classify_review_change(
        commit_id="commit-2",
        local="present",
        pull_request_lookup=None,
        remote_target="commit-1",
    )

    assert status.remote_branch == "drifted"
    assert status.remote_branch_matches_commit is False


def test_classifier_reports_unknown_review_decision_when_lookup_errors() -> None:
    status = classify_review_change(
        commit_id="commit-1",
        local="present",
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=_pull_request(),
            review_decision=None,
            review_decision_error="GitHub returned 502",
            state="open",
        ),
        remote_target=None,
        review_identity=_identity(),
    )

    assert status.pr_lifecycle == "open"
    assert status.pr_review_decision == "unknown"
    assert status.pr_review_decision_error == "GitHub returned 502"
    assert status.has_pull_request_lookup_failure is True
