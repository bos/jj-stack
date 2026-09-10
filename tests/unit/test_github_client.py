from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx2
import pytest

from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import CommitId


def _github_client(handler) -> GithubClient:
    return GithubClient(
        httpx2.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx2.MockTransport(handler),
        ),
        repo=GithubRepoAddress(
            owner="octo-org",
            repo="stacked-prs",
        ),
    )


def test_github_client_retries_429_responses_with_retry_after() -> None:
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx2.Response(
                429,
                headers={"Retry-After": "0"},
                json={"message": "slow down"},
                request=request,
            )
        return httpx2.Response(
            200,
            json={
                "default_branch": "main",
                "full_name": "octo-org/stacked-prs",
            },
            request=request,
        )

    async def run_test() -> str:
        async with _github_client(handler) as client:
            repo = await client.get_repo()
        return repo.full_name

    assert asyncio.run(run_test()) == "octo-org/stacked-prs"
    assert attempts == 2


def test_github_client_caps_and_announces_a_long_rate_limit_wait(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slept: list[float] = []
    attempts = 0

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx2.Response(
                429,
                headers={"Retry-After": "3600"},
                json={"message": "API rate limit exceeded"},
                request=request,
            )
        return httpx2.Response(
            200,
            json={"default_branch": "main", "full_name": "octo-org/stacked-prs"},
            request=request,
        )

    async def run_test() -> str:
        async with _github_client(handler) as client:
            return (await client.get_repo()).full_name

    with caplog.at_level(logging.WARNING, logger="jj_stack.github.client"):
        assert asyncio.run(run_test()) == "octo-org/stacked-prs"

    assert slept == [60.0]
    assert "GitHub rate limit reached" in caplog.text


def test_github_client_retries_secondary_rate_limits_without_retry_after() -> None:
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx2.Response(
                403,
                headers={"X-RateLimit-Reset": "0"},
                json={"message": "You have exceeded a secondary rate limit."},
                request=request,
            )
        return httpx2.Response(
            200,
            json={
                "default_branch": "main",
                "full_name": "octo-org/stacked-prs",
            },
            request=request,
        )

    async def run_test() -> str:
        async with _github_client(handler) as client:
            repo = await client.get_repo()
        return repo.default_branch or ""

    assert asyncio.run(run_test()) == "main"
    assert attempts == 2


def test_github_client_does_not_retry_non_rate_limited_errors() -> None:
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(404, json={"message": "Not Found"}, request=request)

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_repo()

    with pytest.raises(GithubClientError, match="GitHub request failed: 404"):
        asyncio.run(run_test())

    assert attempts == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    (
        ("<html>Proxy authentication required</html>", "was not valid JSON"),
        ('{"full_name": null}', "had invalid data"),
    ),
)
def test_github_client_rejects_an_unusable_success_response(body: str, reason: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            text=body,
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_repo()

    with pytest.raises(GithubClientError, match=f"repo lookup response {reason}"):
        asyncio.run(run_test())


@pytest.mark.parametrize(
    ("base", "body", "title", "expected_payload"),
    (
        (None, "new body", "new title", {"body": "new body", "title": "new title"}),
        ("main", None, None, {"base": "main"}),
        ("main", "", "new title", {"base": "main", "body": "", "title": "new title"}),
    ),
)
def test_github_client_sends_only_supplied_pr_updates(
    base: str | None,
    body: str | None,
    title: str | None,
    expected_payload: dict[str, str],
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert json.loads(request.content.decode("utf-8")) == expected_payload
        return httpx2.Response(
            200,
            json={
                "base": {"ref": base or "old-base"},
                "body": body or "",
                "head": {"ref": "jj-stack/feature", "sha": "head-commit"},
                "html_url": "https://github.test/octo-org/stacked-prs/pull/7",
                "node_id": "PR_7",
                "number": 7,
                "state": "open",
                "title": title or "old title",
            },
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.update_pr(
                pr_number=7,
                base=base,
                body=body,
                title=title,
            )

    asyncio.run(run_test())


def test_github_client_distinguishes_dissolved_and_locked_stack() -> None:
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        assert request.method == "POST"
        assert request.url.path == "/repos/octo-org/stacked-prs/stacks/3/unstack"
        if attempts == 1:
            return httpx2.Response(204, request=request)
        return httpx2.Response(
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
        return dissolved, remaining.pr_numbers

    assert asyncio.run(run_test()) == (None, (8, 9))


def test_github_client_paginates_stack_list() -> None:
    def _stack(number: int, *pr_numbers: int) -> dict[str, object]:
        return {
            "number": number,
            "pull_requests": [
                {
                    "head": {"ref": f"jj-stack/{pr_number}", "sha": f"head-{pr_number}"},
                    "merged_at": None,
                    "number": pr_number,
                    "state": "open",
                }
                for pr_number in pr_numbers
            ],
        }

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/repos/octo-org/stacked-prs/stacks"
        if request.url.params.get("page") == "2":
            return httpx2.Response(200, json=[_stack(2, 20, 21)], request=request)
        return httpx2.Response(
            200,
            headers={
                "Link": (
                    "<https://api.github.test/repos/octo-org/stacked-prs/stacks?page=2>; "
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


def test_github_client_fails_the_whole_stack_listing_on_an_unexpected_payload() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json=[{"number": 3, "pull_requests": [{"number": "not-a-number"}]}],
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.list_stacks()

    with pytest.raises(GithubClientError, match="unusable data for stack #3"):
        asyncio.run(run_test())


def test_github_client_batches_pr_lookup_by_number_with_graphql() -> None:
    request_sizes: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-prs"}
        request_sizes.append(payload["query"].count("pullRequest(number:"))
        if len(request_sizes) == 1:
            assert "pr_7: pullRequest(number: 7)" in payload["query"]
            assert "pr_9: pullRequest(number: 9)" in payload["query"]
            assert "pr_11: pullRequest(number: 11)" in payload["query"]
            assert "autoMergeRequest" not in payload["query"]
            assert "mergeQueueEntry" in payload["query"]
            assert "statusCheckRollup" in payload["query"]
        return httpx2.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": {
                            "autoMergeRequest": None,
                            "baseRefName": "main",
                            "body": "body 7",
                            "headRefOid": "head-commit",
                            "headRefName": "jj-stack/seven",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergeQueueEntry": {"id": "queue-entry"},
                            "mergedAt": None,
                            "id": "PR_7",
                            "number": 7,
                            "state": "OPEN",
                            "statusCheckRollup": {"state": "SUCCESS"},
                            "title": "seven",
                            "url": "https://github.test/octo-org/stacked-prs/pull/7",
                        },
                        "pr_9": {
                            "autoMergeRequest": None,
                            "baseRefName": "jj-stack/base",
                            "body": None,
                            "headRefOid": "head-commit",
                            "headRefName": "jj-stack/nine",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergeQueueEntry": None,
                            "mergedAt": "2026-03-16T12:00:00Z",
                            "id": "PR_9",
                            "number": 9,
                            "state": "MERGED",
                            "title": "nine",
                            "url": "https://github.test/octo-org/stacked-prs/pull/9",
                        },
                        "pr_11": None,
                    }
                },
                # GitHub reports an unresolvable alias as `null` in `data` *and* an error.
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "path": ["repository", "pr_11"],
                        "message": "Could not resolve to a PullRequest with the number of 11.",
                    }
                ],
            },
            request=request,
        )

    async def run_test() -> tuple[str, str, str | None, bool, str | None, bool]:
        async with _github_client(handler) as client:
            prs = await client.get_prs_by_numbers(
                pr_numbers=(7, 9, 11, *range(100, 124)),
            )
        pr_7 = prs[7]
        pr_9 = prs[9]
        if pr_7 is None or pr_9 is None:
            raise AssertionError("GraphQL lookup should return both pull requests.")
        return (
            pr_7.head.ref,
            pr_9.state,
            pr_7.head.label,
            pr_7.is_queued,
            pr_7.check_rollup_status,
            prs[11] is None,
        )

    assert asyncio.run(run_test()) == (
        "jj-stack/seven",
        "merged",
        "octo-org:jj-stack/seven",
        True,
        "passed",
        True,
    )
    assert request_sizes == [25, 2]


def test_github_client_observes_exact_and_suffix_matched_branch_targets() -> None:
    suffix_queries: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        query = payload["query"]
        if "BranchTargetsBySuffix" in query:
            suffix_queries.append(query)
            variables = payload["variables"]
            assert variables["owner"] == "octo-org"
            assert variables["repo"] == "stacked-prs"
            assert variables["suffix_0"] == "-aaaaaaaa"
            assert variables["ref_prefix"] == "refs/heads/jj-stack/"
            assert "query: $suffix_0" in query
            assert "refPrefix: $ref_prefix" in query
            if "after: $cursor_0" in query:
                assert variables["cursor_0"] == "page-1"
                nodes = [
                    {
                        "name": "old-slug-aaaaaaaa",
                        "prefix": "refs/heads/jj-stack/",
                        "target": {"oid": "old-target"},
                    }
                ]
                page_info = {"endCursor": "page-2", "hasNextPage": False}
            else:
                nodes = [
                    {
                        "name": "contains-aaaaaaaa-elsewhere",
                        "prefix": "refs/heads/jj-stack/",
                        "target": {"oid": "unrelated-target"},
                    }
                ]
                page_info = {"endCursor": "page-1", "hasNextPage": True}
            repo = {
                "suffix_0": {
                    "nodes": nodes,
                    "pageInfo": page_info,
                }
            }
        else:
            assert payload["variables"] == {
                "owner": "octo-org",
                "repo": "stacked-prs",
                "qualified_0": "refs/heads/jj-stack/current",
                "qualified_1": "refs/heads/jj-stack/missing",
            }
            assert "ref(qualifiedName: $qualified_0)" in query
            assert "ref(qualifiedName: $qualified_1)" in query
            repo = {
                "branch_0": {
                    "name": "jj-stack/current",
                    "prefix": "refs/heads/",
                    "target": {"oid": "current-target"},
                },
                "branch_1": None,
            }
        return httpx2.Response(
            200,
            json={"data": {"repository": repo}},
            request=request,
        )

    async def run_test() -> tuple[dict[str, CommitId], dict[str, CommitId]]:
        async with _github_client(handler) as client:
            exact = await client.get_branch_targets(
                branches=("jj-stack/current", "jj-stack/missing"),
            )
            recovered = await client.find_branch_targets_by_suffix(
                branch_prefix="jj-stack/",
                suffixes=("-aaaaaaaa",),
            )
        return exact, recovered

    assert asyncio.run(run_test()) == (
        {"jj-stack/current": "current-target"},
        {"jj-stack/old-slug-aaaaaaaa": "old-target"},
    )
    assert len(suffix_queries) == 2


def test_github_client_detects_merge_queue_branch_rule() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {
            "owner": "octo-org",
            "repo": "stacked-prs",
            "branch": "main",
            "qualified": "refs/heads/main",
        }
        return httpx2.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "mergeQueue": None,
                        "ref": {"rules": {"nodes": [{"type": "MERGE_QUEUE"}]}},
                    }
                }
            },
            request=request,
        )

    async def run_test() -> bool:
        async with _github_client(handler) as client:
            return await client.base_branch_uses_merge_queue(branch="main")

    assert asyncio.run(run_test())


@pytest.mark.parametrize(
    ("error_entry", "expected_detail"),
    (
        pytest.param(
            {
                "type": "NOT_FOUND",
                "path": ["repository"],
                "message": (
                    "Could not resolve to a Repository with the name 'octo-org/stacked-prs'."
                ),
            },
            "Could not resolve to a Repository",
            id="not-found-for-the-repository-itself",
        ),
        pytest.param(
            {
                "type": "FORBIDDEN",
                "path": ["repository", "pr_7"],
                "message": "Resource not accessible by personal access token.",
            },
            "Resource not accessible",
            id="another-error-type-on-a-tolerated-path",
        ),
    ),
)
def test_github_client_fails_closed_on_graphql_errors_that_are_not_a_missing_alias(
    error_entry: dict[str, object],
    expected_detail: str,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"data": {"repository": None}, "errors": [error_entry]},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_prs_by_numbers(pr_numbers=(7,))

    with pytest.raises(GithubClientError) as raised:
        asyncio.run(run_test())

    assert expected_detail in str(raised.value)
    # `is_repo_not_found` keys on NOT_FOUND at the repository path, so only the first row
    # reports it.
    assert raised.value.is_repo_not_found() == (error_entry["type"] == "NOT_FOUND")


def test_github_client_rejects_graphql_payload_missing_repo_data() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/graphql"
        return httpx2.Response(
            200,
            json={"data": {}},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_prs_by_numbers(
                pr_numbers=(7,),
            )

    with pytest.raises(GithubClientError, match="missing repo data"):
        asyncio.run(run_test())


@pytest.mark.parametrize(
    "repo_payload",
    ({}, {"base_0": {}}),
)
def test_github_client_rejects_incomplete_pr_connection(
    repo_payload: dict[str, object],
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/graphql"
        return httpx2.Response(
            200,
            json={"data": {"repository": repo_payload}},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_prs_by_base_refs(
                base_refs=("jj-stack/seven",),
            )

    with pytest.raises(GithubClientError, match="invalid connection payload"):
        asyncio.run(run_test())


def test_github_client_batches_open_pr_lookup_by_head_ref_with_graphql() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {
            "owner": "octo-org",
            "repo": "stacked-prs",
            "ref_0": "jj-stack/nine",
            "ref_1": "jj-stack/seven",
        }
        assert "headRefName: $ref_0" in payload["query"]
        assert "headRefName: $ref_1" in payload["query"]
        assert "headRepositoryOwner" in payload["query"]
        assert "reviewDecision" in payload["query"]
        assert "states: [OPEN]" in payload["query"]
        return httpx2.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "head_0": {
                            "nodes": [
                                {
                                    "baseRefName": "jj-stack/base",
                                    "body": None,
                                    "headRefOid": "head-commit",
                                    "headRefName": "jj-stack/nine",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": None,
                                    "id": "PR_9",
                                    "number": 9,
                                    "state": "OPEN",
                                    "title": "nine",
                                    "url": "https://github.test/octo-org/stacked-prs/pull/9",
                                }
                            ]
                        },
                        "head_1": {
                            "nodes": [
                                {
                                    "baseRefName": "main",
                                    "body": "body 7",
                                    "headRefOid": "head-commit",
                                    "headRefName": "jj-stack/seven",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": None,
                                    "id": "PR_7",
                                    "number": 7,
                                    "reviewDecision": "APPROVED",
                                    "state": "OPEN",
                                    "title": "seven",
                                    "url": "https://github.test/octo-org/stacked-prs/pull/7",
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
            prs = await client.get_open_prs_by_head_refs(
                head_refs=("jj-stack/seven", "jj-stack/nine"),
            )
        pr_7 = prs["jj-stack/seven"][0]
        pr_9 = prs["jj-stack/nine"][0]
        return (
            pr_7.head.ref,
            pr_9.state,
            pr_7.head.label,
            pr_7.review_decision,
        )

    assert asyncio.run(run_test()) == (
        "jj-stack/seven",
        "open",
        "octo-org:jj-stack/seven",
        "approved",
    )


def test_github_client_paginates_comments_and_skips_unavailable_revisions() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        # The second request carries the cursor the first page returned.
        next_page = payload["variables"].get("comments_cursor_7") == "comments-1"
        pr_payload: dict[str, object] = {
            "comments": {
                "nodes": [
                    {
                        "body": (
                            "<!-- jj-stack-overview -->" if next_page else "ordinary comment"
                        ),
                        "databaseId": 71 if next_page else 70,
                    }
                ],
                "pageInfo": {
                    "endCursor": None if next_page else "comments-1",
                    "hasNextPage": not next_page,
                },
            }
        }
        if not next_page:
            pr_payload["timelineItems"] = {
                "filteredCount": 4,
                "nodes": [
                    {
                        "afterCommit": {"oid": "22222222"},
                        "beforeCommit": {"oid": "11111111"},
                    },
                    {
                        "afterCommit": {"oid": "33333333"},
                        "beforeCommit": None,
                    },
                    {
                        "afterCommit": None,
                        "beforeCommit": {"oid": "33333333"},
                    },
                    {
                        "afterCommit": {"oid": "55555555"},
                        "beforeCommit": {"oid": "44444444"},
                    },
                ],
            }
        return httpx2.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": pr_payload,
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[int | None, list[tuple[int, str, str, bool]]]:
        async with _github_client(handler) as client:
            comments, revisions = await client.find_issue_comments_and_revisions(
                body_markers=("<!-- jj-stack-overview -->",),
                pr_numbers=(7,),
                revision_limit=7,
            )
        comment = comments["<!-- jj-stack-overview -->"][7]
        return (
            comment.id if comment is not None else None,
            [
                (
                    revision.version,
                    revision.before_commit_id,
                    revision.commit_id,
                    revision.is_current,
                )
                for revision in revisions[7]
            ],
        )

    assert asyncio.run(run_test()) == (
        71,
        [
            (2, "11111111", "22222222", False),
            (5, "44444444", "55555555", True),
        ],
    )


def test_github_client_filters_batched_head_lookup_results_to_repo_owner() -> None:
    def _node(number: int, owner: str) -> dict[str, object]:
        return {
            "baseRefName": "main",
            "body": "body",
            "headRefOid": "head-commit",
            "headRefName": "jj-stack/seven",
            "headRepositoryOwner": {"login": owner},
            "mergedAt": None,
            "id": f"PR_{number}",
            "number": number,
            "state": "OPEN",
            "title": f"pr {number}",
            "url": f"https://github.test/octo-org/stacked-prs/pull/{number}",
        }

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {
            "owner": "octo-org",
            "repo": "stacked-prs",
            "ref_0": "jj-stack/seven",
        }
        # GitHub returns fork pull requests on a same-named branch too, oldest first, and
        # truncates the page to `first` before jj-stack can filter by head owner.
        first = int(payload["query"].partition("first:")[2].partition(",")[0])
        nodes = [
            _node(5, "fork-user"),
            _node(6, "other-fork-user"),
            _node(7, "octo-org"),
        ]
        return httpx2.Response(
            200,
            json={"data": {"repository": {"head_0": {"nodes": nodes[:first]}}}},
            request=request,
        )

    async def run_test() -> list[int]:
        async with _github_client(handler) as client:
            prs = await client.get_open_prs_by_head_refs(
                head_refs=("jj-stack/seven",),
            )
        return [pr.number for pr in prs["jj-stack/seven"]]

    assert asyncio.run(run_test()) == [7]


def test_user_facing_reason_quotes_githubs_message_for_a_404_without_raw_detail() -> None:
    # The raw response body (JSON, network phrasing) must never leak into the user-facing
    # reason, and a bare 404 says only what GitHub said: the caller knows what was looked up.
    error = GithubClientError(
        "GitHub request failed: 404",
        body='{"message":"Not Found","documentation_url":"x"}',
        status_code=404,
    )

    reason = error.user_facing_reason()

    assert reason == "request failed (GitHub 404: Not Found)"
    assert "documentation_url" not in reason
    assert not error.is_repo_not_found()


@pytest.mark.parametrize(
    ("body", "expected_reason"),
    (
        pytest.param(
            {
                "message": "Validation Failed",
                "errors": [
                    {
                        "resource": "PullRequest",
                        "code": "custom",
                        "message": "A pull request already exists for octo-org:jj-stack/x.",
                    }
                ],
            },
            "request failed (GitHub 422: Validation Failed: A pull request already exists for "
            "octo-org:jj-stack/x.)",
            id="quotes-githubs-json-explanation",
        ),
        pytest.param(
            "<html><body>Blocked by proxy</body></html>",
            "request failed (GitHub 422)",
            id="ignores-a-body-that-is-not-githubs-json",
        ),
    ),
)
def test_github_client_quotes_githubs_own_explanation_for_a_refusal(
    body: dict[str, object] | str,
    expected_reason: str,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if isinstance(body, str):
            return httpx2.Response(422, request=request, text=body)
        return httpx2.Response(422, request=request, json=body)

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.create_pr(base="main", body="", head="jj-stack/x", title="x")

    with pytest.raises(GithubClientError) as raised:
        asyncio.run(run_test())

    assert raised.value.user_facing_reason() == expected_reason


def test_user_facing_reason_reports_auth_failure_for_401() -> None:
    error = GithubClientError("GitHub request failed: 401", status_code=401)

    assert error.user_facing_reason() == "auth failed - check GITHUB_TOKEN"


@pytest.mark.parametrize(
    ("headers", "expected_reason"),
    (
        pytest.param(
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "+3600"},
            "GitHub primary rate limit reached, resets in about 60 min - rerun later",
            id="primary-limit-from-the-remaining-header",
        ),
        pytest.param(
            {"Retry-After": "60"},
            "GitHub secondary rate limit reached, resets in about 1 min - rerun later",
            id="secondary-limit-from-retry-after",
        ),
    ),
)
def test_github_client_reports_an_exhausted_rate_limit_as_a_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    expected_reason: str,
) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    resolved = {
        name: (str(int(time.time()) + int(value[1:])) if value.startswith("+") else value)
        for name, value in headers.items()
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        # A body that says nothing about rate limits: the retry loop reads the headers, so
        # the diagnostic has to read the same verdict rather than the prose.
        return httpx2.Response(
            403,
            headers=resolved,
            json={"message": "Forbidden"},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_repo()

    with pytest.raises(GithubClientError) as raised:
        asyncio.run(run_test())

    assert raised.value.user_facing_reason() == expected_reason


def test_github_client_reports_a_permissions_403_as_access_denied() -> None:
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        # GitHub sends the quota headers on nearly every response, this one included, so a
        # rate-limit verdict must not be read from their mere presence.
        return httpx2.Response(
            403,
            headers={
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4998",
                "X-RateLimit-Reset": str(int(time.time()) + 3600),
            },
            json={"message": "Resource not accessible by personal access token"},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_repo()

    with pytest.raises(GithubClientError) as raised:
        asyncio.run(run_test())

    assert (
        raised.value.user_facing_reason() == "access denied - check GITHUB_TOKEN and repo access"
    )
    # Waiting cannot fix a permissions failure, so it must not be retried either.
    assert attempts == 1
