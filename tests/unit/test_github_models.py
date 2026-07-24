from __future__ import annotations

import pytest

from jj_stack.models.github import GithubPullRequest, GithubStack


def _graphql_pull_request_payload(review_decision: object) -> dict[str, object]:
    return {
        "autoMergeRequest": None,
        "baseRefName": "main",
        "headRefName": "review/feature-1",
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


def test_native_stack_retains_member_state_and_rejects_nonprefix_history() -> None:
    historical = {
        "head": {"ref": "review/one", "sha": "head-one"},
        "merged_at": "2026-07-23T12:00:00Z",
        "number": 1,
        "state": "closed",
    }
    active = {
        "head": {"ref": "review/two", "sha": "head-two"},
        "merged_at": None,
        "number": 2,
        "state": "open",
    }

    stack = GithubStack.model_validate(
        {"number": 7, "pull_requests": [historical, active]}
    )

    assert stack.historical_pull_request_numbers == (1,)
    assert stack.active_pull_request_numbers == (2,)
    assert stack.active_pull_requests[0].head.model_dump() == {
        "ref": "review/two",
        "sha": "head-two",
    }
    with pytest.raises(ValueError, match="bottom prefix"):
        GithubStack.model_validate(
            {"number": 7, "pull_requests": [active, historical]}
        )
