from __future__ import annotations

import asyncio
from pathlib import Path

import httpxyz
import pytest

from jj_stack.commands._github_stack_support import resolve_github_stack_support
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubRepository
from jj_stack.state.store import ReviewStateStore


def _github_client(handler) -> GithubClient:
    return GithubClient(
        httpxyz.AsyncClient(
            base_url="https://api.github.test",
            transport=httpxyz.MockTransport(handler),
        ),
        repository=GithubRepoAddress(
            host="github.test",
            owner="local-name",
            repo="stacked-review",
        ),
    )


def _github_repository() -> GithubRepository:
    return GithubRepository(
        clone_url="https://github.test/octo-org/stacked-review.git",
        default_branch="main",
        full_name="Octo-Org/Stacked-Review",
        html_url="https://github.test/octo-org/stacked-review",
        name="stacked-review",
        private=True,
        url="https://api.github.test/repos/octo-org/stacked-review",
    )


def test_stack_support_caches_supported_repository_and_reuses_observation(
    tmp_path: Path,
) -> None:
    requests = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal requests
        requests += 1
        return httpxyz.Response(
            200,
            json=[{"number": 3, "pull_requests": [{"number": 7}, {"number": 8}]}],
            request=request,
        )

    async def run_test():
        state_path = tmp_path / "state.json"
        async with _github_client(handler) as client:
            detected = await resolve_github_stack_support(
                github_client=client,
                github_repository=_github_repository(),
                state_store=ReviewStateStore(state_path),
            )
            cached = await resolve_github_stack_support(
                github_client=client,
                github_repository=_github_repository(),
                state_store=ReviewStateStore(state_path),
            )
        return detected, cached

    detected, cached = asyncio.run(run_test())

    assert detected.supported is True
    assert detected.observed_stacks is not None
    assert detected.observed_stacks[0].pull_request_numbers == (7, 8)
    assert cached.supported is True
    assert cached.observed_stacks is None
    assert requests == 1


def test_stack_support_caches_conclusive_404_as_unsupported(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal requests
        requests += 1
        return httpxyz.Response(404, json={"message": "Not Found"}, request=request)

    async def run_test():
        state_path = tmp_path / "state.json"
        async with _github_client(handler) as client:
            detected = await resolve_github_stack_support(
                github_client=client,
                github_repository=_github_repository(),
                state_store=ReviewStateStore(state_path),
            )
            cached = await resolve_github_stack_support(
                github_client=client,
                github_repository=_github_repository(),
                state_store=ReviewStateStore(state_path),
            )
        return detected, cached

    detected, cached = asyncio.run(run_test())

    assert detected.supported is False
    assert cached.supported is False
    assert requests == 1


def test_stack_support_does_not_cache_uncertain_failure(tmp_path: Path) -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        return httpxyz.Response(500, json={"message": "try again"}, request=request)

    state_path = tmp_path / "state.json"

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await resolve_github_stack_support(
                github_client=client,
                github_repository=_github_repository(),
                state_store=ReviewStateStore(state_path),
            )

    with pytest.raises(GithubClientError, match="GitHub request failed: 500"):
        asyncio.run(run_test())

    assert (
        ReviewStateStore(state_path).get_stacked_pull_requests(
            "github.test/octo-org/stacked-review"
        )
        is None
    )


@pytest.mark.parametrize(
    ("response_kind", "error_pattern"),
    (
        ("invalid_stack", "invalid stack data"),
        ("invalid_json", "not valid JSON"),
    ),
)
def test_stack_support_classifies_malformed_response_without_caching(
    tmp_path: Path,
    response_kind: str,
    error_pattern: str,
) -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        if response_kind == "invalid_json":
            return httpxyz.Response(200, text="{", request=request)
        return httpxyz.Response(
            200,
            json=[{"number": 3, "pull_requests": [{}]}],
            request=request,
        )

    state_path = tmp_path / "state.json"

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await resolve_github_stack_support(
                github_client=client,
                github_repository=_github_repository(),
                state_store=ReviewStateStore(state_path),
            )

    with pytest.raises(GithubClientError, match=error_pattern):
        asyncio.run(run_test())

    assert (
        ReviewStateStore(state_path).get_stacked_pull_requests(
            "github.test/octo-org/stacked-review"
        )
        is None
    )


def test_stack_support_dry_run_observes_without_caching(tmp_path: Path) -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        return httpxyz.Response(200, json=[], request=request)

    state_path = tmp_path / "state.json"

    async def run_test():
        async with _github_client(handler) as client:
            return await resolve_github_stack_support(
                github_client=client,
                github_repository=_github_repository(),
                state_store=ReviewStateStore(state_path),
                persist=False,
            )

    support = asyncio.run(run_test())

    assert support.supported is True
    assert support.observed_stacks == ()
    assert (
        ReviewStateStore(state_path).get_stacked_pull_requests(
            "github.test/octo-org/stacked-review"
        )
        is None
    )
