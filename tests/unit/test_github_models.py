from __future__ import annotations

import pytest

from jj_stack.models.github import GithubPullRequest, GithubStack


def _graphql_pull_request_payload(review_decision: object) -> dict[str, object]:
    return {
        "autoMergeRequest": None,
        "baseRefName": "main",
        "headRefName": "jj-stack/feature-1",
        "headRefOid": "head-commit-id",
        "headRepositoryOwner": {"login": "octo-org"},
        "mergeQueueEntry": None,
        "number": 1,
        "reviewDecision": review_decision,
        "state": "OPEN",
        "title": "feature 1",
        "url": "https://github.test/octo-org/stacked-review/pull/1",
    }


def test_graphql_review_decision_normalizes_known_states_and_drops_unknown() -> None:
    approved = GithubPullRequest.model_validate(_graphql_pull_request_payload("APPROVED"))
    changes = GithubPullRequest.model_validate(_graphql_pull_request_payload("CHANGES_REQUESTED"))
    unknown = GithubPullRequest.model_validate(_graphql_pull_request_payload("REVIEW_REQUIRED"))

    assert approved.review_decision == "approved"
    assert approved.head.sha == "head-commit-id"
    assert changes.review_decision == "changes_requested"
    assert unknown.review_decision is None


def test_github_stack_splits_history_and_rejects_nonprefix_history() -> None:
    historical = {
        "head": {"ref": "jj-stack/one", "sha": "head-one"},
        "merged_at": "2026-07-23T12:00:00Z",
        "number": 1,
    }
    active = {
        "head": {"ref": "jj-stack/two", "sha": "head-two"},
        "merged_at": None,
        "number": 2,
    }

    stack = GithubStack.model_validate({"number": 7, "pull_requests": [historical, active]})

    assert stack.historical_pull_request_numbers == (1,)
    assert stack.active_pull_request_numbers == (2,)
    with pytest.raises(ValueError, match="bottom prefix"):
        GithubStack.model_validate({"number": 7, "pull_requests": [active, historical]})


def test_github_stack_requires_two_members_and_defaults_missing_merge_state_to_active() -> None:
    """GitHub rejects one-member resources and may omit `merged_at` from valid members."""

    stack = GithubStack.model_validate(
        {
            "number": 7,
            "pull_requests": [
                {"head": {"ref": "jj-stack/one", "sha": "head-one"}, "number": 1},
                {"head": {"ref": "jj-stack/two", "sha": "head-two"}, "number": 2},
            ],
        }
    )

    assert stack.active_pull_request_numbers == (1, 2)
    assert stack.historical_pull_request_numbers == ()
    with pytest.raises(ValueError, match="at least 2"):
        GithubStack.model_validate(
            {
                "number": 8,
                "pull_requests": [
                    {"head": {"ref": "jj-stack/one", "sha": "head-one"}, "number": 1}
                ],
            }
        )
