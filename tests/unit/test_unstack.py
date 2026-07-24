from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.commands.unstack import (
    PreparedClose,
    _close_revision_preflight_error,
    _CloseMutationRun,
    unstack,
)
from jj_stack.errors import UsageError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.change_status import ReviewChangeStatus
from jj_stack.review.status import ReviewStatusRevision

CHANGE_ID = "aaaaaaaaaaaaaaaa"
BRANCH = "review/feature-aaaaaaaa"


def test_unstack_rejects_orphans_without_cleanup_before_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr(
        "jj_stack.commands.unstack.bootstrap_context",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid options must not bootstrap the repository")
        ),
    )

    with pytest.raises(UsageError, match="orphans requires --cleanup"):
        unstack(
            cleanup=False,
            cli_args=JjCliArgs(),
            debug=False,
            dry_run=False,
            local=False,
            pull_request="orphans",
            repository=None,
            revset=None,
        )


def test_unstack_blocks_saved_identity_from_another_repository() -> None:
    identity = _review_identity().model_copy(update={"repository_name": "other-repository"})
    revision = replace(_stub_revision(), review_identity=identity)
    action = _close_revision_preflight_error(
        change_status=ReviewChangeStatus(
            local="present",
            remote_branch="current",
            remote_branch_matches_commit=True,
            pr_lifecycle="open",
            pr_draft=False,
            pr_review_decision="none",
            saved_review_identity=True,
        ),
        revision=revision,
        run=_CloseMutationRun(
            current_state=ReviewState(
                review_identities={CHANGE_ID: identity},
                submitted_baselines={CHANGE_ID: SubmittedBaseline(commit_id="commit-1")},
            ),
            github_client=cast(GithubClient, SimpleNamespace()),
            initial_observation=None,
            planned_closed_pull_requests=set(),
            review_identities={CHANGE_ID: identity},
            prepared_close=cast(
                PreparedClose,
                SimpleNamespace(
                    cleanup=False,
                    dry_run=False,
                    prepared_status=SimpleNamespace(
                        github_repository=GithubRepoAddress(
                            host="github.test",
                            owner="octo-org",
                            repo="stacked-review",
                        ),
                    ),
                ),
            ),
            record_action=lambda _action: None,
        ),
    )

    assert action is not None
    assert action.status == "blocked"
    assert "saved repository does not match" in action.message


def test_selected_cleanup_rejects_saved_branch_that_does_not_match_change() -> None:
    identity = _review_identity().model_copy(update={"head_ref": "review/different-bbbbbbbb"})
    revision = replace(_stub_revision(), review_identity=identity)
    action = _close_revision_preflight_error(
        change_status=ReviewChangeStatus(
            local="present",
            remote_branch="current",
            remote_branch_matches_commit=True,
            pr_lifecycle="closed",
            pr_draft=None,
            pr_review_decision="none",
            saved_review_identity=True,
        ),
        revision=revision,
        run=_CloseMutationRun(
            current_state=ReviewState(
                review_identities={CHANGE_ID: identity},
                submitted_baselines={CHANGE_ID: SubmittedBaseline(commit_id="commit-1")},
            ),
            github_client=cast(GithubClient, SimpleNamespace()),
            initial_observation=None,
            planned_closed_pull_requests=set(),
            review_identities={CHANGE_ID: identity},
            prepared_close=cast(
                PreparedClose,
                SimpleNamespace(
                    cleanup=True,
                    dry_run=False,
                    prepared_status=SimpleNamespace(
                        github_repository=GithubRepoAddress(
                            host="github.test",
                            owner="octo-org",
                            repo="stacked-review",
                        ),
                    ),
                ),
            ),
            record_action=lambda _action: None,
        ),
    )

    assert action is not None
    assert action.kind == "tracking"
    assert "does not match change" in action.message


def _review_identity() -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=1,
        head_owner="octo-org",
        head_ref=BRANCH,
    )


def _stub_revision() -> ReviewStatusRevision:
    return ReviewStatusRevision(
        branch=BRANCH,
        change_id=CHANGE_ID,
        commit_id="commit-1",
        local_divergent=False,
        pull_request_lookup=None,
        review_identity=_review_identity(),
        remote_target="commit-1",
        submitted_baseline=SubmittedBaseline(commit_id="commit-1"),
        managed_comments_lookup=None,
        subject="feature",
    )
