from __future__ import annotations

from jj_stack.models.github import GithubPullRequest


def _graphql_pull_request_payload(review_decision: object) -> dict[str, object]:
    return {
        "baseRefName": "main",
        "headRefName": "review/feature-1",
        "headRepositoryOwner": {"login": "octo-org"},
        "number": 1,
        "reviewDecision": review_decision,
        "state": "OPEN",
        "title": "feature 1",
        "url": "https://github.test/octo-org/stacked-review/pull/1",
    }


def test_graphql_review_decision_normalizes_known_states_and_drops_unknown() -> None:
    approved = GithubPullRequest.model_validate(_graphql_pull_request_payload("APPROVED"))
    changes = GithubPullRequest.model_validate(
        _graphql_pull_request_payload("CHANGES_REQUESTED")
    )
    unknown = GithubPullRequest.model_validate(_graphql_pull_request_payload("REVIEW_REQUIRED"))

    assert approved.review_decision == "approved"
    assert changes.review_decision == "changes_requested"
    assert unknown.review_decision is None
