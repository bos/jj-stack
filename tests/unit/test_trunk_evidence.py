from __future__ import annotations

import pytest

from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubBranchRef, GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.stack.trunk_evidence import classify_exact_snapshot, classify_rewritten_result


def _candidate() -> TrackedPR:
    return TrackedPR(
        change_id="change-1",
        pr_identity=PRIdentity(
            repo_owner="octo-org",
            repo_name="stacked-prs",
            pr_number=1,
            head_owner="octo-org",
            head_ref="jj-stack/change-1",
        ),
        submitted_baseline=SubmittedBaseline(commit_id="submitted-1"),
    )


def _pr(**updates: object) -> GithubPR:
    pr = GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(
            label="octo-org:jj-stack/change-1",
            ref="jj-stack/change-1",
            sha="submitted-1",
        ),
        html_url="https://github.test/octo-org/stacked-prs/pull/1",
        merged_at=None,
        number=1,
        state="open",
        title="change 1",
    )
    return pr.model_copy(update=updates)


@pytest.mark.merge_recovery
def test_exact_snapshot_evidence_is_identity_and_ancestry_bound() -> None:
    rows = (
        ("on_trunk", _pr(), "octo-org", True, False),
        ("not_on_trunk", _pr(), "octo-org", False, False),
        ("unresolved", _pr(), "octo-org", False, False),
        (
            "on_trunk",
            _pr(head=GithubBranchRef(ref="other", sha="submitted-1")),
            "octo-org",
            False,
            True,
        ),
        ("on_trunk", _pr(), "other-org", False, True),
        (
            "on_trunk",
            _pr(
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

    for ancestry, pr, owner, on_trunk, pr_mismatch in rows:
        result = classify_exact_snapshot(
            ancestry=ancestry,
            candidate=_candidate(),
            pr=pr,
            repo=GithubRepoAddress(
                owner=owner,
                repo="stacked-prs",
            ),
        )

        assert result.on_trunk is on_trunk
        assert result.pr_mismatch is pr_mismatch
        # An unproven verdict always explains itself, so no caller has to invent a message.
        assert on_trunk or result.reason is not None


@pytest.mark.merge_recovery
def test_rewritten_result_requires_a_reachable_concrete_merge_result() -> None:
    rows = (
        (
            _pr(head=GithubBranchRef(ref="other", sha="submitted-1")),
            None,
            False,
        ),
        (
            _pr(
                head=GithubBranchRef(
                    label="octo-org:jj-stack/change-1",
                    ref="jj-stack/change-1",
                    sha="other",
                )
            ),
            None,
            False,
        ),
        (_pr(), None, False),
        (
            _pr(state="closed", merged_at="2026-07-21T12:00:00Z"),
            None,
            False,
        ),
        (
            _pr(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "unresolved",
            False,
        ),
        (
            _pr(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "not_on_trunk",
            False,
        ),
        (
            _pr(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "on_trunk",
            True,
        ),
    )

    for pr, ancestry, on_trunk in rows:
        result = classify_rewritten_result(
            candidate=_candidate(),
            merge_result_ancestry=ancestry,
            pr=pr,
            repo=GithubRepoAddress(
                owner="octo-org",
                repo="stacked-prs",
            ),
        )

        assert result.on_trunk is on_trunk
        assert on_trunk or result.reason is not None


def _change(*, commit_id: str, immutable: bool = False) -> LocalCommit:
    return LocalCommit(
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

    assert not _change(commit_id="submitted-1").holds_unpublished_edit(published)
    assert _change(commit_id="edited-locally").holds_unpublished_edit(published)
    # An immutable change cannot hold a local edit, whatever its commit.
    assert not _change(commit_id="edited-locally", immutable=True).holds_unpublished_edit(
        published
    )
    # Adopting a GitHub-stack survivor also counts the commit GitHub reported for it.
    assert not _change(commit_id="github-rewrote-this").holds_unpublished_edit(
        ("submitted-1", "github-rewrote-this")
    )
