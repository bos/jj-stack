from __future__ import annotations

from jj_review.models.bookmarks import RemoteBookmarkState
from jj_review.models.github import GithubBranchRef, GithubPullRequest
from jj_review.models.review_state import CachedChange
from jj_review.review.change_status import (
    SubmittedStateDisagreement,
    classify_review_change,
)
from jj_review.review.status import PullRequestLookup


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


def test_classifier_keeps_draft_and_review_decision_as_separate_axes() -> None:
    status = classify_review_change(
        cached_change=CachedChange(pr_number=1, pr_state="open"),
        commit_id="commit-1",
        local="present",
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=_pull_request(draft=True),
            review_decision="approved",
            review_decision_error=None,
            state="open",
        ),
        remote_state=None,
    )

    assert status.pr_lifecycle == "open"
    assert status.pr_draft is True
    assert status.pr_review_decision == "approved"


def test_classifier_marks_missing_lookup_with_saved_pr_identity_as_stale_link() -> None:
    status = classify_review_change(
        cached_change=CachedChange(pr_number=1, pr_state="open"),
        commit_id="commit-1",
        local="present",
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=None,
            review_decision=None,
            review_decision_error=None,
            state="missing",
        ),
        remote_state=None,
    )

    assert status.pr_lifecycle == "missing"
    assert status.has_stale_pull_request_link is True


def test_classifier_keeps_untracked_remote_branch_distinct_from_current() -> None:
    untracked_status = classify_review_change(
        cached_change=CachedChange(bookmark="review/change"),
        commit_id="commit-1",
        local="present",
        pull_request_lookup=None,
        remote_state=RemoteBookmarkState(
            remote="origin",
            targets=("commit-1",),
            tracking_targets=(),
        ),
    )
    current_status = classify_review_change(
        cached_change=CachedChange(bookmark="review/change"),
        commit_id="commit-1",
        local="present",
        pull_request_lookup=None,
        remote_state=RemoteBookmarkState(
            remote="origin",
            targets=("commit-1",),
            tracking_targets=("commit-1",),
        ),
    )

    assert untracked_status.remote_branch == "untracked"
    assert current_status.remote_branch == "current"


def test_classifier_preserves_independent_baseline_flags() -> None:
    status = classify_review_change(
        baseline_disagreement=SubmittedStateDisagreement(
            change_id="change-a",
            commit_changed=True,
            parent_changed=True,
            stack_head_changed=False,
        ),
        cached_change=CachedChange(last_submitted_commit_id="old"),
        commit_id="new",
        local="present",
        pull_request_lookup=None,
        remote_state=None,
    )

    assert status.baseline == frozenset({"commit_changed", "parent_changed"})
