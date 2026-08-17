from __future__ import annotations

from jj_stack.models.github import GithubBranchRef, GithubPR
from jj_stack.models.tracking import PRIdentity
from jj_stack.stack.change_status import (
    classify_change_status,
)
from jj_stack.stack.status import PRLookup


def _pr(*, draft: bool = False, state: str = "open") -> GithubPR:
    merged_at = "2026-05-09T12:00:00Z" if state == "merged" else None
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        draft=draft,
        head=GithubBranchRef(ref="jj-stack/change"),
        html_url="https://github.test/octo/repo/pull/1",
        merged_at=merged_at,
        number=1,
        state="closed" if state == "merged" else state,
        title="change",
    ).normalize_state()


def _identity(*, pr_number: int = 1) -> PRIdentity:
    return PRIdentity(
        repo_owner="octo-org",
        repo_name="stacked-prs",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref="jj-stack/change",
    )


def test_classifier_keeps_draft_and_review_decision_as_separate_axes() -> None:
    status = classify_change_status(
        local="present",
        pr_lookup=PRLookup(
            message=None,
            pr=_pr(draft=True),
            review_decision="approved",
            review_decision_error=None,
            state="open",
        ),
        pr_identity=_identity(),
    )

    assert status.pr_lifecycle == "open"
    assert status.pr_draft is True
    assert status.pr_review_decision == "approved"


def test_classifier_marks_missing_lookup_with_saved_pr_identity_as_stale_link() -> None:
    status = classify_change_status(
        local="present",
        pr_lookup=PRLookup(
            message=None,
            pr=None,
            review_decision=None,
            review_decision_error=None,
            state="missing",
        ),
        pr_identity=_identity(),
    )

    assert status.pr_lifecycle == "missing"
    assert status.has_stale_pr_link is True


def test_classifier_reports_saved_pr_identity() -> None:
    status = classify_change_status(
        local="present",
        pr_lookup=None,
        pr_identity=_identity(),
    )

    assert status.saved_pr_identity is True


def test_classifier_reports_unknown_review_decision_when_lookup_errors() -> None:
    status = classify_change_status(
        local="present",
        pr_lookup=PRLookup(
            message=None,
            pr=_pr(),
            review_decision=None,
            review_decision_error="GitHub returned 502",
            state="open",
        ),
        pr_identity=_identity(),
    )

    assert status.pr_lifecycle == "open"
    assert status.pr_review_decision == "unknown"
    assert status.pr_review_decision_error == "GitHub returned 502"
    assert status.has_pr_lookup_failure is True
