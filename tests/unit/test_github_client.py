from __future__ import annotations

import asyncio
import json

import httpxyz
import pytest

from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubPullRequest


def _github_client(handler, *, client_type=GithubClient) -> GithubClient:
    return client_type(
        httpxyz.AsyncClient(
            base_url="https://api.github.test",
            transport=httpxyz.MockTransport(handler),
        ),
        repository=GithubRepoAddress(
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
                "default_branch": "main",
                "full_name": "octo-org/stacked-review",
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
                "default_branch": "main",
                "full_name": "octo-org/stacked-review",
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


@pytest.mark.parametrize(
    ("base", "body", "title", "expected_payload"),
    (
        (None, "new body", "new title", {"body": "new body", "title": "new title"}),
        ("main", None, None, {"base": "main"}),
        ("main", "", "new title", {"base": "main", "body": "", "title": "new title"}),
    ),
)
def test_github_client_sends_only_supplied_pull_request_updates(
    base: str | None,
    body: str | None,
    title: str | None,
    expected_payload: dict[str, str],
) -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert json.loads(request.content.decode("utf-8")) == expected_payload
        return httpxyz.Response(
            200,
            json={
                "base": {"ref": base or "old-base"},
                "body": body or "",
                "head": {"ref": "jj-stack/feature"},
                "html_url": "https://github.test/octo-org/stacked-review/pull/7",
                "number": 7,
                "state": "open",
                "title": title or "old title",
            },
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.update_pull_request(
                pull_number=7,
                base=base,
                body=body,
                title=title,
            )

    asyncio.run(run_test())


def test_github_client_distinguishes_dissolved_and_locked_stack() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        assert request.method == "POST"
        assert request.url.path == "/repos/octo-org/stacked-review/stacks/3/unstack"
        if attempts == 1:
            return httpxyz.Response(204, request=request)
        return httpxyz.Response(
            200,
            json={
                "number": 3,
                "pull_requests": [
                    {
                        "head": {"ref": "jj-stack/eight", "sha": "head-eight"},
                        "merged_at": None,
                        "number": 8,
                        "state": "open",
                    },
                    {
                        "head": {"ref": "jj-stack/nine", "sha": "head-nine"},
                        "merged_at": None,
                        "number": 9,
                        "state": "open",
                    },
                ],
            },
            request=request,
        )

    async def run_test() -> tuple[object, tuple[int, ...]]:
        async with _github_client(handler) as client:
            dissolved = await client.unstack(stack_number=3)
            remaining = await client.unstack(stack_number=3)
        if remaining is None:
            raise AssertionError("The second unstack should return its locked member.")
        return dissolved, remaining.pull_request_numbers

    assert asyncio.run(run_test()) == (None, (8, 9))


def test_github_client_paginates_stack_list() -> None:
    def _stack(number: int, *pull_numbers: int) -> dict[str, object]:
        return {
            "number": number,
            "pull_requests": [
                {
                    "head": {"ref": f"jj-stack/{pull_number}", "sha": f"head-{pull_number}"},
                    "merged_at": None,
                    "number": pull_number,
                    "state": "open",
                }
                for pull_number in pull_numbers
            ],
        }

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/repos/octo-org/stacked-review/stacks"
        if request.url.params.get("page") == "2":
            return httpxyz.Response(200, json=[_stack(2, 20, 21)], request=request)
        return httpxyz.Response(
            200,
            headers={
                "Link": (
                    "<https://api.github.test/repos/octo-org/stacked-review/stacks?page=2>; "
                    'rel="next"'
                )
            },
            json=[_stack(1, 10, 11)],
            request=request,
        )

    async def run_test() -> tuple[int, ...]:
        async with _github_client(handler) as client:
            return tuple(stack.number for stack in await client.list_stacks())

    assert asyncio.run(run_test()) == (1, 2)


def test_github_client_batches_pull_request_lookup_by_number_with_graphql() -> None:
    request_sizes: list[int] = []

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-review"}
        request_sizes.append(payload["query"].count("pullRequest(number:"))
        if len(request_sizes) == 1:
            assert "pr_7: pullRequest(number: 7)" in payload["query"]
            assert "pr_9: pullRequest(number: 9)" in payload["query"]
            assert "pr_11: pullRequest(number: 11)" in payload["query"]
            assert "autoMergeRequest" not in payload["query"]
            assert "mergeQueueEntry" not in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": {
                            "autoMergeRequest": None,
                            "baseRefName": "main",
                            "body": "body 7",
                            "headRefName": "jj-stack/seven",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergeQueueEntry": None,
                            "mergedAt": None,
                            "number": 7,
                            "state": "OPEN",
                            "title": "seven",
                            "url": "https://github.test/octo-org/stacked-review/pull/7",
                        },
                        "pr_9": {
                            "autoMergeRequest": None,
                            "baseRefName": "jj-stack/base",
                            "body": None,
                            "headRefName": "jj-stack/nine",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergeQueueEntry": None,
                            "mergedAt": "2026-03-16T12:00:00Z",
                            "number": 9,
                            "state": "CLOSED",
                            "title": "nine",
                            "url": "https://github.test/octo-org/stacked-review/pull/9",
                        },
                        "pr_11": None,
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[str, str, str | None, bool]:
        async with _github_client(handler) as client:
            pull_requests = await client.get_pull_requests_by_numbers(
                pull_numbers=(7, 9, 11, *range(100, 124)),
            )
        pull_request_7 = pull_requests[7]
        pull_request_9 = pull_requests[9]
        if pull_request_7 is None or pull_request_9 is None:
            raise AssertionError("GraphQL lookup should return both pull requests.")
        return (
            pull_request_7.head.ref,
            pull_request_9.state,
            pull_request_7.head.label,
            pull_requests[11] is None,
        )

    assert asyncio.run(run_test()) == (
        "jj-stack/seven",
        "closed",
        "octo-org:jj-stack/seven",
        True,
    )
    assert request_sizes == [25, 2]


@pytest.mark.merge_recovery
def test_github_client_bounds_independent_pull_request_fallbacks() -> None:
    class FallbackClient(GithubClient):
        active = 0
        max_active = 0

        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            raise GithubClientError("forced batch failure")

        async def get_pull_request(self, *, pull_number):
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            await asyncio.sleep(0)
            try:
                if pull_number == 1:
                    raise GithubClientError("one failed")
                if pull_number == 2:
                    return GithubPullRequest.model_validate({})
                return GithubPullRequest(
                    base={"ref": "main"},
                    head={"ref": f"jj-stack/{pull_number}"},
                    html_url=f"https://github.test/pull/{pull_number}",
                    number=pull_number,
                    state="open",
                    title=f"PR {pull_number}",
                )
            finally:
                type(self).active -= 1

    def unused_handler(request: httpxyz.Request) -> httpxyz.Response:
        raise AssertionError(f"unexpected transport request: {request.url}")

    async def run_test():
        async with _github_client(unused_handler, client_type=FallbackClient) as client:
            return await client.get_pull_requests_by_numbers_independently(
                pull_numbers=tuple(range(1, 18)),
            )

    results = asyncio.run(run_test())

    assert isinstance(results[1], GithubClientError)
    assert isinstance(results[2], GithubClientError)
    assert isinstance(results[3], GithubPullRequest)
    assert FallbackClient.max_active == DEFAULT_BOUNDED_CONCURRENCY


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


@pytest.mark.parametrize(
    "repository_payload",
    ({}, {"base_0": {}}),
)
def test_github_client_rejects_incomplete_pull_request_connection(
    repository_payload: dict[str, object],
) -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        return httpxyz.Response(
            200,
            json={"data": {"repository": repository_payload}},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_open_pull_requests_by_base_refs(
                base_refs=("jj-stack/seven",),
            )

    with pytest.raises(GithubClientError, match="invalid connection payload"):
        asyncio.run(run_test())


def test_github_client_batches_pull_request_lookup_by_head_ref_with_graphql() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-review"}
        assert 'headRefName: "jj-stack/seven"' in payload["query"]
        assert 'headRefName: "jj-stack/nine"' in payload["query"]
        assert "headRepositoryOwner" in payload["query"]
        assert "reviewDecision" in payload["query"]
        assert "states: [OPEN, CLOSED, MERGED]" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "head_0": {
                            "nodes": [
                                {
                                    "baseRefName": "jj-stack/base",
                                    "body": None,
                                    "headRefName": "jj-stack/nine",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": "2026-03-16T12:00:00Z",
                                    "number": 9,
                                    "state": "MERGED",
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
                                    "headRefName": "jj-stack/seven",
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
                head_refs=("jj-stack/seven", "jj-stack/nine"),
            )
        pull_request_7 = pull_requests["jj-stack/seven"][0]
        pull_request_9 = pull_requests["jj-stack/nine"][0]
        return (
            pull_request_7.head.ref,
            pull_request_9.state,
            pull_request_7.head.label,
            pull_request_7.review_decision,
        )

    assert asyncio.run(run_test()) == (
        "jj-stack/seven",
        "merged",
        "octo-org:jj-stack/seven",
        "approved",
    )


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
                                        "body": "<!-- jj-stack-overview -->",
                                        "databaseId": 70,
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

    async def run_test() -> int:
        async with _github_client(handler) as client:
            comments = await client.get_issue_comments_by_pull_request_numbers(
                pull_numbers=(7,),
            )
        return comments[7][0].id

    assert asyncio.run(run_test()) == 70
    assert len(queries) == 1


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
                                    "headRefName": "jj-stack/seven",
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
                                    "headRefName": "jj-stack/seven",
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
                head_refs=("jj-stack/seven",),
            )
        return [pull_request.number for pull_request in pull_requests["jj-stack/seven"]]

    assert asyncio.run(run_test()) == [7]


def test_user_facing_reason_reports_repo_not_found_for_404_without_raw_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend a token is configured so the 404 reason omits the auth follow-up
    # hint, letting us assert the bare repo-not-found wording. The raw response
    # body (JSON, network phrasing) must never leak into the user-facing reason.
    monkeypatch.setattr("jj_stack.github.client.github_token_from_env", lambda: "token")
    error = GithubClientError(
        'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
        status_code=404,
    )

    reason = error.user_facing_reason()

    assert reason == "repo not found or inaccessible"
    assert "documentation_url" not in reason
    assert "network" not in reason


def test_user_facing_reason_reports_auth_failure_for_401() -> None:
    error = GithubClientError("GitHub request failed: 401", status_code=401)

    assert error.user_facing_reason() == "auth failed - check GITHUB_TOKEN"


def test_user_facing_reason_reports_access_denied_for_403() -> None:
    error = GithubClientError("GitHub request failed: 403", status_code=403)

    assert error.user_facing_reason() == "access denied - check GITHUB_TOKEN and repo access"
