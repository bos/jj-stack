from __future__ import annotations

from jj_stack.models.github import GithubPullRequest


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
    auto_merge_payload = _graphql_pull_request_payload(None) | {
        "autoMergeRequest": {"enabledAt": "now"}
    }
    queue_payload = _graphql_pull_request_payload(None) | {"mergeQueueEntry": {"id": "entry"}}
    incomplete_payload = _graphql_pull_request_payload(None)
    del incomplete_payload["mergeQueueEntry"]

    assert approved.review_decision == "approved"
    assert approved.head.sha == "head-commit-id"
    assert approved.landing_owners == frozenset()
    assert changes.review_decision == "changes_requested"
    assert unknown.review_decision is None
    assert GithubPullRequest.model_validate(auto_merge_payload).landing_owners == {"auto_merge"}
    assert GithubPullRequest.model_validate(queue_payload).landing_owners == {"merge_queue"}
    assert GithubPullRequest.model_validate(incomplete_payload).landing_owners is None
