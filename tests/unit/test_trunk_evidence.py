from __future__ import annotations

import pytest

from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline, TrackedReview
from jj_stack.models.stack import LocalRevision
from jj_stack.review.trunk_evidence import classify_exact_snapshot, classify_rewritten_result


def _candidate() -> TrackedReview:
    return TrackedReview(
        change_id="change-1",
        review_identity=ReviewIdentity(
            repository_owner="octo-org",
            repository_name="stacked-review",
            pr_number=1,
            head_owner="octo-org",
            head_ref="jj-stack/change-1",
        ),
        submitted_baseline=SubmittedBaseline(commit_id="submitted-1"),
    )


def _pull_request(**updates: object) -> GithubPullRequest:
    pull_request = GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(
            label="octo-org:jj-stack/change-1",
            ref="jj-stack/change-1",
            sha="submitted-1",
        ),
        html_url="https://github.test/octo-org/stacked-review/pull/1",
        merged_at=None,
        number=1,
        state="open",
        title="change 1",
    )
    return pull_request.model_copy(update=updates)


@pytest.mark.merge_recovery
def test_exact_snapshot_evidence_is_identity_and_ancestry_bound() -> None:
    rows = (
        ("on_trunk", _pull_request(), "octo-org", True, False),
        ("not_on_trunk", _pull_request(), "octo-org", False, False),
        ("unresolved", _pull_request(), "octo-org", False, False),
        (
            "on_trunk",
            _pull_request(head=GithubBranchRef(ref="other", sha="submitted-1")),
            "octo-org",
            False,
            True,
        ),
        ("on_trunk", _pull_request(), "other-org", False, True),
        (
            "on_trunk",
            _pull_request(
                head=GithubBranchRef(
                    label="octo-org:jj-stack/change-1",
                    ref="jj-stack/change-1",
                    sha="other",
                )
            ),
            "octo-org",
            False,
            True,
        ),
    )

    for ancestry, pull_request, owner, on_trunk, review_mismatch in rows:
        result = classify_exact_snapshot(
            ancestry=ancestry,
            candidate=_candidate(),
            pull_request=pull_request,
            repository=GithubRepoAddress(
                owner=owner,
                repo="stacked-review",
            ),
        )

        assert result.on_trunk is on_trunk
        assert result.review_mismatch is review_mismatch
        # An unproven verdict always explains itself, so no caller has to invent a message.
        assert on_trunk or result.reason is not None


@pytest.mark.merge_recovery
def test_rewritten_result_requires_a_reachable_concrete_merge_result() -> None:
    rows = (
        (
            _pull_request(head=GithubBranchRef(ref="other", sha="submitted-1")),
            None,
            False,
        ),
        (
            _pull_request(
                head=GithubBranchRef(
                    label="octo-org:jj-stack/change-1",
                    ref="jj-stack/change-1",
                    sha="other",
                )
            ),
            None,
            False,
        ),
        (_pull_request(), None, False),
        (
            _pull_request(state="closed", merged_at="2026-07-21T12:00:00Z"),
            None,
            False,
        ),
        (
            _pull_request(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "unresolved",
            False,
        ),
        (
            _pull_request(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "not_on_trunk",
            False,
        ),
        (
            _pull_request(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "on_trunk",
            True,
        ),
    )

    for pull_request, ancestry, on_trunk in rows:
        result = classify_rewritten_result(
            candidate=_candidate(),
            merge_result_ancestry=ancestry,
            pull_request=pull_request,
            repository=GithubRepoAddress(
                owner="octo-org",
                repo="stacked-review",
            ),
        )

        assert result.on_trunk is on_trunk
        assert on_trunk or result.reason is not None


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
    """One wrong answer here destroys local work, so pin every shape callers pass."""

    published = ("submitted-1",)

    assert not _revision(commit_id="submitted-1").holds_unpublished_edit(published)
    assert _revision(commit_id="edited-locally").holds_unpublished_edit(published)
    # An immutable revision cannot hold a local edit, whatever its commit.
    assert not _revision(commit_id="edited-locally", immutable=True).holds_unpublished_edit(
        published
    )
    # Adopting a GitHub-stack survivor also counts the commit GitHub reported for it.
    assert not _revision(commit_id="github-rewrote-this").holds_unpublished_edit(
        ("submitted-1", "github-rewrote-this")
    )
