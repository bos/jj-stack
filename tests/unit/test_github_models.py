from __future__ import annotations

from jj_stack.models.github import GithubPR, GithubStack


def _graphql_pr_payload(
    review_decision: object,
    *,
    check_rollup_state: object = None,
) -> dict[str, object]:
    return {
        "autoMergeRequest": None,
        "baseRefName": "main",
        "headRefName": "jj-stack/feature-1",
        "headRefOid": "head-commit-id",
        "headRepositoryOwner": {"login": "octo-org"},
        "mergeQueueEntry": None,
        "id": "PR_1",
        "number": 1,
        "reviewDecision": review_decision,
        "state": "OPEN",
        "statusCheckRollup": (
            None if check_rollup_state is None else {"state": check_rollup_state}
        ),
        "title": "feature 1",
        "url": "https://github.test/octo-org/stacked-prs/pull/1",
    }


def test_graphql_pr_statuses_normalize_known_states_and_drop_unknown() -> None:
    approved = GithubPR.model_validate(
        _graphql_pr_payload("APPROVED", check_rollup_state="SUCCESS")
    )
    changes = GithubPR.model_validate(
        _graphql_pr_payload("CHANGES_REQUESTED", check_rollup_state="FAILURE")
    )
    errored = GithubPR.model_validate(_graphql_pr_payload(None, check_rollup_state="ERROR"))
    pending = GithubPR.model_validate(_graphql_pr_payload(None, check_rollup_state="PENDING"))
    expected = GithubPR.model_validate(_graphql_pr_payload(None, check_rollup_state="EXPECTED"))
    required = GithubPR.model_validate(_graphql_pr_payload("REVIEW_REQUIRED"))
    unknown = GithubPR.model_validate(
        _graphql_pr_payload("FUTURE_STATE", check_rollup_state="FUTURE_STATE")
    )

    assert approved.review_decision == "approved"
    assert approved.check_rollup_status == "passed"
    assert approved.head.sha == "head-commit-id"
    assert changes.review_decision == "changes_requested"
    assert changes.check_rollup_status == "failed"
    assert errored.check_rollup_status == "failed"
    assert pending.check_rollup_status == "pending"
    assert expected.check_rollup_status == "pending"
    assert required.review_decision == "review_required"
    assert unknown.review_decision is None
    assert unknown.check_rollup_status is None


def test_rest_merged_prs_have_the_same_state_as_graphql() -> None:
    merged_at = "2026-07-23T12:00:00Z"
    rest = GithubPR.model_validate(
        {
            "base": {"ref": "main"},
            "head": {"ref": "jj-stack/feature-1", "sha": "head-commit-id"},
            "html_url": "https://github.test/octo-org/stacked-prs/pull/1",
            "merged_at": merged_at,
            "node_id": "PR_1",
            "number": 1,
            "state": "closed",
            "title": "feature 1",
        }
    )
    graphql = GithubPR.model_validate(
        _graphql_pr_payload(None) | {"state": "MERGED", "mergedAt": merged_at}
    )

    assert rest.state == graphql.state == "merged"


def test_github_stack_splits_history_and_reports_a_merged_member_above_an_active_one() -> None:
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

    assert stack.active_pr_numbers == (2,)
    assert stack.has_merged_prefix
    reversed_stack = GithubStack.model_validate(
        {"number": 7, "pull_requests": [active, historical]}
    )
    assert not reversed_stack.has_merged_prefix


def test_github_stack_defaults_missing_merge_state_to_active() -> None:
    stack = GithubStack.model_validate(
        {
            "number": 7,
            "pull_requests": [
                {"head": {"ref": "jj-stack/one", "sha": "head-one"}, "number": 1},
                {"head": {"ref": "jj-stack/two", "sha": "head-two"}, "number": 2},
            ],
        }
    )

    assert stack.active_pr_numbers == (1, 2)
