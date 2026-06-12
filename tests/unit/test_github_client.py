from __future__ import annotations

import asyncio
import json

import httpxyz
import pytest

from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import ParsedGithubRepo


def _github_client(handler) -> GithubClient:
    return GithubClient(
        httpxyz.AsyncClient(
            base_url="https://api.github.test",
            transport=httpxyz.MockTransport(handler),
        ),
        repository=ParsedGithubRepo(
            host="github.test",
            owner="octo-org",
            repo="stacked-review",
        ),
    )


def test_github_client_retries_429_responses_with_retry_after() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpxyz.Response(
                429,
                headers={"Retry-After": "0"},
                json={"message": "slow down"},
                request=request,
            )
        return httpxyz.Response(
            200,
            json={
                "clone_url": "https://github.test/octo-org/stacked-review.git",
                "default_branch": "main",
                "full_name": "octo-org/stacked-review",
                "html_url": "https://github.test/octo-org/stacked-review",
                "name": "stacked-review",
                "private": True,
                "url": "https://api.github.test/repos/octo-org/stacked-review",
            },
            request=request,
        )

    async def run_test() -> str:
        async with _github_client(handler) as client:
            repository = await client.get_repository()
        return repository.full_name

    assert asyncio.run(run_test()) == "octo-org/stacked-review"
    assert attempts == 2


def test_github_client_retries_secondary_rate_limits_without_retry_after() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpxyz.Response(
                403,
                headers={"X-RateLimit-Reset": "0"},
                json={"message": "You have exceeded a secondary rate limit."},
                request=request,
            )
        return httpxyz.Response(
            200,
            json={
                "clone_url": "https://github.test/octo-org/stacked-review.git",
                "default_branch": "main",
                "full_name": "octo-org/stacked-review",
                "html_url": "https://github.test/octo-org/stacked-review",
                "name": "stacked-review",
                "private": True,
                "url": "https://api.github.test/repos/octo-org/stacked-review",
            },
            request=request,
        )

    async def run_test() -> str:
        async with _github_client(handler) as client:
            repository = await client.get_repository()
        return repository.default_branch or ""

    assert asyncio.run(run_test()) == "main"
    assert attempts == 2


def test_github_client_does_not_retry_non_rate_limited_errors() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        return httpxyz.Response(404, json={"message": "Not Found"}, request=request)

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_repository()

    with pytest.raises(GithubClientError, match="GitHub request failed: 404"):
        asyncio.run(run_test())

    assert attempts == 1


def test_github_client_lists_pull_request_reviews() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/repos/octo-org/stacked-review/pulls/7/reviews"
        if request.url.params.get("page") == "2":
            return httpxyz.Response(
                200,
                json=[
                    {
                        "id": 2,
                        "state": "COMMENTED",
                        "user": {"login": "reviewer-2"},
                    }
                ],
                request=request,
            )
        return httpxyz.Response(
            200,
            headers={
                "Link": (
                    "<https://api.github.test/repos/octo-org/stacked-review/pulls/7/reviews"
                    '?page=2>; rel="next"'
                )
            },
            json=[
                {
                    "id": 1,
                    "state": "APPROVED",
                    "user": {"login": "reviewer-1"},
                }
            ],
            request=request,
        )

    async def run_test() -> tuple[str, str]:
        async with _github_client(handler) as client:
            reviews = await client.list_pull_request_reviews(
                pull_number=7,
            )
        if reviews[0].user is None:
            raise AssertionError("Review payload should include a user.")
        return reviews[0].user.login, reviews[1].state

    assert asyncio.run(run_test()) == ("reviewer-1", "COMMENTED")


def test_github_client_paginates_pull_request_list() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/repos/octo-org/stacked-review/pulls"
        if request.url.params.get("page") == "2":
            return httpxyz.Response(
                200,
                json=[
                    {
                        "base": {"label": "octo-org/stacked-review:main", "ref": "main"},
                        "head": {"label": "octo-org:review/two", "ref": "review/two"},
                        "html_url": "https://github.test/octo-org/stacked-review/pull/2",
                        "merged_at": None,
                        "number": 2,
                        "state": "open",
                        "title": "two",
                    }
                ],
                request=request,
            )
        return httpxyz.Response(
            200,
            headers={
                "Link": (
                    "<https://api.github.test/repos/octo-org/stacked-review/pulls?page=2>; "
                    'rel="next"'
                )
            },
            json=[
                {
                    "base": {"label": "octo-org/stacked-review:main", "ref": "main"},
                    "head": {"label": "octo-org:review/one", "ref": "review/one"},
                    "html_url": "https://github.test/octo-org/stacked-review/pull/1",
                    "merged_at": None,
                    "number": 1,
                    "state": "open",
                    "title": "one",
                }
            ],
            request=request,
        )

    async def run_test() -> tuple[int, int]:
        async with _github_client(handler) as client:
            pull_requests = await client.list_pull_requests(
                head="octo-org:review/one",
            )
        return pull_requests[0].number, pull_requests[1].number

    assert asyncio.run(run_test()) == (1, 2)


def test_github_client_batches_pull_request_lookup_by_number_with_graphql() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-review"}
        assert "pr_7: pullRequest(number: 7)" in payload["query"]
        assert "pr_9: pullRequest(number: 9)" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": {
                            "baseRefName": "main",
                            "body": "body 7",
                            "headRefName": "review/seven",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergedAt": None,
                            "number": 7,
                            "state": "OPEN",
                            "title": "seven",
                            "url": "https://github.test/octo-org/stacked-review/pull/7",
                        },
                        "pr_9": {
                            "baseRefName": "review/base",
                            "body": None,
                            "headRefName": "review/nine",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergedAt": "2026-03-16T12:00:00Z",
                            "number": 9,
                            "state": "CLOSED",
                            "title": "nine",
                            "url": "https://github.test/octo-org/stacked-review/pull/9",
                        },
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[str, str, str | None]:
        async with _github_client(handler) as client:
            pull_requests = await client.get_pull_requests_by_numbers(
                pull_numbers=(7, 9),
            )
        pull_request_7 = pull_requests[7]
        pull_request_9 = pull_requests[9]
        if pull_request_7 is None or pull_request_9 is None:
            raise AssertionError("GraphQL lookup should return both pull requests.")
        return pull_request_7.head.ref, pull_request_9.state, pull_request_7.head.label

    assert asyncio.run(run_test()) == (
        "review/seven",
        "closed",
        "octo-org:review/seven",
    )


def test_github_client_rejects_graphql_payload_missing_repository_data() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        return httpxyz.Response(
            200,
            json={"data": {}},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_pull_requests_by_numbers(
                pull_numbers=(7,),
            )

    with pytest.raises(GithubClientError, match="missing repository data"):
        asyncio.run(run_test())


def test_github_client_batches_pull_request_lookup_by_head_ref_with_graphql() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-review"}
        assert 'headRefName: "review/seven"' in payload["query"]
        assert 'headRefName: "review/nine"' in payload["query"]
        assert "headRepositoryOwner" in payload["query"]
        assert "reviewDecision" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "head_0": {
                            "nodes": [
                                {
                                    "baseRefName": "review/base",
                                    "body": None,
                                    "headRefName": "review/nine",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": "2026-03-16T12:00:00Z",
                                    "number": 9,
                                    "state": "CLOSED",
                                    "title": "nine",
                                    "url": "https://github.test/octo-org/stacked-review/pull/9",
                                }
                            ]
                        },
                        "head_1": {
                            "nodes": [
                                {
                                    "baseRefName": "main",
                                    "body": "body 7",
                                    "headRefName": "review/seven",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": None,
                                    "number": 7,
                                    "reviewDecision": "APPROVED",
                                    "state": "OPEN",
                                    "title": "seven",
                                    "url": "https://github.test/octo-org/stacked-review/pull/7",
                                }
                            ]
                        },
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[str, str, str | None, str | None]:
        async with _github_client(handler) as client:
            pull_requests = await client.get_pull_requests_by_head_refs(
                head_refs=("review/seven", "review/nine"),
            )
        pull_request_7 = pull_requests["review/seven"][0]
        pull_request_9 = pull_requests["review/nine"][0]
        return (
            pull_request_7.head.ref,
            pull_request_9.state,
            pull_request_7.head.label,
            pull_request_7.review_decision,
        )

    assert asyncio.run(run_test()) == (
        "review/seven",
        "closed",
        "octo-org:review/seven",
        "approved",
    )


def test_github_client_batches_review_decision_lookup_with_graphql() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-review"}
        assert "pr_7: pullRequest(number: 7)" in payload["query"]
        assert "pr_9: pullRequest(number: 9)" in payload["query"]
        assert "latestOpinionatedReviews" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": {
                            "latestOpinionatedReviews": {
                                "nodes": [
                                    {
                                        "author": {"login": "reviewer-1"},
                                        "state": "APPROVED",
                                    },
                                    {
                                        "author": {"login": "reviewer-2"},
                                        "state": "CHANGES_REQUESTED",
                                    },
                                ]
                            }
                        },
                        "pr_9": {
                            "latestOpinionatedReviews": {
                                "nodes": [
                                    {
                                        "author": {"login": "reviewer-3"},
                                        "state": "DISMISSED",
                                    }
                                ]
                            }
                        },
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[str | None, str | None]:
        async with _github_client(handler) as client:
            decisions = await client.get_review_decisions_by_pull_request_numbers(
                pull_numbers=(7, 9),
            )
        return decisions[7], decisions[9]

    assert asyncio.run(run_test()) == ("changes_requested", None)


def test_github_client_loads_issue_comments_with_graphql() -> None:
    queries: list[str] = []

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        queries.append(payload["query"])
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-review"}
        assert "pr_7: pullRequest(number: 7)" in payload["query"]
        assert "comments(first: 100)" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": {
                            "comments": {
                                "nodes": [
                                    {
                                        "body": "<!-- jj-stack-navigation -->",
                                        "databaseId": 70,
                                        "url": "https://github.test/comment/70",
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False},
                            }
                        },
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[int, str]:
        async with _github_client(handler) as client:
            comments = await client.get_issue_comments_by_pull_request_numbers(
                pull_numbers=(7,),
            )
        return comments[7][0].id, comments[7][0].html_url

    assert asyncio.run(run_test()) == (70, "https://github.test/comment/70")
    assert len(queries) == 1


def test_github_client_converts_pull_request_to_draft_via_graphql() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.method == "POST"
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"pullRequestId": "PR_kwDOA7"}
        assert "convertPullRequestToDraft" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "convertPullRequestToDraft": {
                        "pullRequest": {
                            "id": "PR_kwDOA7",
                            "number": 7,
                            "state": "OPEN",
                            "isDraft": True,
                            "mergedAt": None,
                            "url": "https://github.test/octo-org/stacked-review/pull/7",
                            "title": "feature",
                            "body": "body",
                            "baseRefName": "main",
                            "headRefName": "review/feature",
                            "headRepositoryOwner": {"login": "octo-org"},
                        }
                    }
                }
            },
            request=request,
        )

    async def run_test() -> bool:
        async with _github_client(handler) as client:
            pull_request = await client.convert_pull_request_to_draft(
                pull_request_id="PR_kwDOA7",
            )
        return pull_request.is_draft

    assert asyncio.run(run_test()) is True


def test_github_client_marks_pull_request_ready_for_review_via_graphql() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.method == "POST"
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"pullRequestId": "PR_kwDOA7"}
        assert "markPullRequestReadyForReview" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": {
                            "id": "PR_kwDOA7",
                            "number": 7,
                            "state": "OPEN",
                            "isDraft": False,
                            "mergedAt": None,
                            "url": "https://github.test/octo-org/stacked-review/pull/7",
                            "title": "feature",
                            "body": "body",
                            "baseRefName": "main",
                            "headRefName": "review/feature",
                            "headRepositoryOwner": {"login": "octo-org"},
                        }
                    }
                }
            },
            request=request,
        )

    async def run_test() -> bool:
        async with _github_client(handler) as client:
            pull_request = await client.mark_pull_request_ready_for_review(
                pull_request_id="PR_kwDOA7",
            )
        return pull_request.is_draft

    assert asyncio.run(run_test()) is False


def test_github_client_filters_batched_head_lookup_results_to_repo_owner() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-review"}
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "head_0": {
                            "nodes": [
                                {
                                    "baseRefName": "main",
                                    "body": "forked",
                                    "headRefName": "review/seven",
                                    "headRepositoryOwner": {"login": "fork-user"},
                                    "mergedAt": None,
                                    "number": 6,
                                    "state": "OPEN",
                                    "title": "forked",
                                    "url": "https://github.test/octo-org/stacked-review/pull/6",
                                },
                                {
                                    "baseRefName": "main",
                                    "body": "local",
                                    "headRefName": "review/seven",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": None,
                                    "number": 7,
                                    "state": "OPEN",
                                    "title": "local",
                                    "url": "https://github.test/octo-org/stacked-review/pull/7",
                                },
                            ]
                        }
                    }
                }
            },
            request=request,
        )

    async def run_test() -> list[int]:
        async with _github_client(handler) as client:
            pull_requests = await client.get_pull_requests_by_head_refs(
                head_refs=("review/seven",),
            )
        return [pull_request.number for pull_request in pull_requests["review/seven"]]

    assert asyncio.run(run_test()) == [7]
