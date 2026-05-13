"""Minimal async GitHub API client."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from email.utils import parsedate_to_datetime
from typing import Any

import httpxyz
from pydantic import BaseModel, Field, ValidationError

from jj_review.github.auth import github_token_for_base_url
from jj_review.models.github import (
    GithubIssueComment,
    GithubPullRequest,
    GithubPullRequestReview,
    GithubPullRequestReviewUser,
    GithubRepository,
)

logger = logging.getLogger(__name__)
_GRAPHQL_PULL_REQUEST_BATCH_SIZE = 25

_DEFAULT_RATE_LIMIT_RETRIES = 3
_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 1.0
_DEFAULT_MAX_RATE_LIMIT_BACKOFF_SECONDS = 8.0


class GithubClientError(RuntimeError):
    """Raised when GitHub returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class _GraphqlPullRequestConnection(BaseModel):
    nodes: tuple[object, ...] | None = None


class _GraphqlReview(BaseModel):
    author: GithubPullRequestReviewUser | None = None
    state: str


class _GraphqlReviewConnection(BaseModel):
    nodes: tuple[object, ...] | None = None


class _GraphqlIssueCommentConnection(BaseModel):
    nodes: tuple[object | None, ...] | None = None
    page_info: dict[str, object] | None = Field(default=None, alias="pageInfo")


class GithubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(self, client: httpxyz.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> GithubClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_repository(self, owner: str, repo: str) -> GithubRepository:
        response = await self._request("GET", f"/repos/{owner}/{repo}")
        return GithubRepository.model_validate(self._expect_success(response))

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        head: str,
        state: str = "all",
    ) -> tuple[GithubPullRequest, ...]:
        payload = await self._get_paginated_json_array(
            f"/repos/{owner}/{repo}/pulls",
            params={"head": head, "state": state},
            response_name="pull request list",
        )
        return tuple(GithubPullRequest.model_validate(item) for item in payload)

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
    ) -> GithubPullRequest:
        response = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
        )
        return GithubPullRequest.model_validate(self._expect_success(response))

    async def get_pull_requests_by_numbers(
        self,
        owner: str,
        repo: str,
        *,
        pull_numbers: Sequence[int],
    ) -> dict[int, GithubPullRequest | None]:
        numbers = sorted(set(pull_numbers))
        if not numbers:
            return {}

        results: dict[int, GithubPullRequest | None] = {}
        for chunk in _chunked(numbers, size=_GRAPHQL_PULL_REQUEST_BATCH_SIZE):
            query = _pull_requests_by_number_query(chunk)
            payload = await self._graphql_query(
                query,
                variables={"owner": owner, "repo": repo},
                response_name="pull request batch lookup",
            )
            repository = _graphql_repository_payload(
                payload,
                response_name="pull request batch lookup",
            )
            for number in chunk:
                alias = f"pr_{number}"
                raw_pull_request = repository.get(alias)
                if raw_pull_request is None:
                    results[number] = None
                    continue
                results[number] = _validate_graphql_model(
                    raw_pull_request,
                    model=GithubPullRequest,
                    error_message=(
                        "GitHub pull request batch lookup response had invalid pull request "
                        f"payload for #{number}."
                    ),
                )
        return results

    async def get_pull_requests_by_head_refs(
        self,
        owner: str,
        repo: str,
        *,
        head_refs: Sequence[str],
    ) -> dict[str, tuple[GithubPullRequest, ...]]:
        refs = sorted(set(head_refs))
        if not refs:
            return {}

        results: dict[str, tuple[GithubPullRequest, ...]] = {}
        for chunk in _chunked(refs, size=_GRAPHQL_PULL_REQUEST_BATCH_SIZE):
            aliases = {f"head_{index}": head_ref for index, head_ref in enumerate(chunk)}
            query = _pull_requests_by_head_ref_query(aliases)
            payload = await self._graphql_query(
                query,
                variables={"owner": owner, "repo": repo},
                response_name="pull request head lookup",
            )
            repository = _graphql_repository_payload(
                payload,
                response_name="pull request head lookup",
            )
            for alias, head_ref in aliases.items():
                connection = repository.get(alias)
                expected_head_label = f"{owner}:{head_ref}"
                results[head_ref] = _pull_request_connection_from_graphql(
                    alias=alias,
                    connection=connection,
                    expected_head_label=expected_head_label,
                    response_name="pull request head lookup",
                )
        return results

    async def get_review_decisions_by_pull_request_numbers(
        self,
        owner: str,
        repo: str,
        *,
        pull_numbers: Sequence[int],
    ) -> dict[int, str | None]:
        numbers = sorted(set(pull_numbers))
        if not numbers:
            return {}

        results: dict[int, str | None] = {}
        for chunk in _chunked(numbers, size=_GRAPHQL_PULL_REQUEST_BATCH_SIZE):
            query = _pull_request_review_decisions_query(chunk)
            payload = await self._graphql_query(
                query,
                variables={"owner": owner, "repo": repo},
                response_name="pull request review decision lookup",
            )
            repository = _graphql_repository_payload(
                payload,
                response_name="pull request review decision lookup",
            )
            for number in chunk:
                alias = f"pr_{number}"
                raw_pull_request = repository.get(alias)
                results[number] = _review_decision_from_graphql(
                    alias=alias,
                    raw_pull_request=raw_pull_request,
                    response_name="pull request review decision lookup",
                )
        return results

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        base: str,
        body: str,
        draft: bool = False,
        head: str,
        title: str,
    ) -> GithubPullRequest:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={
                "base": base,
                "body": body,
                "draft": draft,
                "head": head,
                "title": title,
            },
        )
        return GithubPullRequest.model_validate(self._expect_success(response))

    async def list_pull_request_reviews(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
    ) -> tuple[GithubPullRequestReview, ...]:
        payload = await self._get_paginated_json_array(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            response_name="pull request reviews",
        )
        return tuple(GithubPullRequestReview.model_validate(item) for item in payload)

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
    ) -> tuple[GithubIssueComment, ...]:
        payload = await self._get_paginated_json_array(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            response_name="issue comment list",
        )
        return tuple(GithubIssueComment.model_validate(item) for item in payload)

    async def get_issue_comments_by_pull_request_numbers(
        self,
        owner: str,
        repo: str,
        *,
        pull_numbers: Sequence[int],
    ) -> dict[int, tuple[GithubIssueComment, ...]]:
        numbers = sorted(set(pull_numbers))
        if not numbers:
            return {}

        results: dict[int, tuple[GithubIssueComment, ...]] = {}
        fallback_numbers: list[int] = []
        for chunk in _chunked(numbers, size=_GRAPHQL_PULL_REQUEST_BATCH_SIZE):
            query = _pull_request_issue_comments_query(chunk)
            payload = await self._graphql_query(
                query,
                variables={"owner": owner, "repo": repo},
                response_name="pull request issue comment lookup",
            )
            repository = _graphql_repository_payload(
                payload,
                response_name="pull request issue comment lookup",
            )
            for number in chunk:
                alias = f"pr_{number}"
                comments, has_next_page = _issue_comments_from_graphql(
                    alias=alias,
                    raw_pull_request=repository.get(alias),
                    response_name="pull request issue comment lookup",
                )
                if has_next_page:
                    fallback_numbers.append(number)
                    continue
                results[number] = comments

        for number in fallback_numbers:
            results[number] = await self.list_issue_comments(
                owner,
                repo,
                issue_number=number,
            )
        return results

    async def create_issue_comment(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
        body: str,
    ) -> GithubIssueComment:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return GithubIssueComment.model_validate(self._expect_success(response))

    async def update_issue_comment(
        self,
        owner: str,
        repo: str,
        *,
        comment_id: int,
        body: str,
    ) -> GithubIssueComment:
        response = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return GithubIssueComment.model_validate(self._expect_success(response))

    async def get_issue_comment(
        self,
        owner: str,
        repo: str,
        *,
        comment_id: int,
    ) -> GithubIssueComment:
        response = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
        )
        return GithubIssueComment.model_validate(self._expect_success(response))

    async def delete_issue_comment(
        self,
        owner: str,
        repo: str,
        *,
        comment_id: int,
    ) -> None:
        response = await self._request(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
        )
        self._expect_no_content(response)

    async def request_reviewers(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
        reviewers: list[str],
        team_reviewers: list[str],
    ) -> None:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers",
            json={"reviewers": reviewers, "team_reviewers": team_reviewers},
        )
        self._expect_success(response)

    async def add_labels(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
        labels: list[str],
    ) -> None:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            json={"labels": labels},
        )
        self._expect_success(response)

    async def update_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
        base: str,
        body: str,
        title: str,
    ) -> GithubPullRequest:
        response = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            json={"base": base, "body": body, "title": title},
        )
        return GithubPullRequest.model_validate(self._expect_success(response))

    async def mark_pull_request_ready_for_review(
        self,
        *,
        pull_request_id: str,
    ) -> GithubPullRequest:
        payload = await self._graphql_query(
            _mark_pull_request_ready_for_review_mutation(),
            response_name="mark pull request ready for review",
            variables={"pullRequestId": pull_request_id},
        )
        return _graphql_mutation_pull_request_payload(
            payload,
            mutation_name="markPullRequestReadyForReview",
            response_name="mark pull request ready for review",
        )

    async def convert_pull_request_to_draft(
        self,
        *,
        pull_request_id: str,
    ) -> GithubPullRequest:
        payload = await self._graphql_query(
            _convert_pull_request_to_draft_mutation(),
            response_name="convert pull request to draft",
            variables={"pullRequestId": pull_request_id},
        )
        return _graphql_mutation_pull_request_payload(
            payload,
            mutation_name="convertPullRequestToDraft",
            response_name="convert pull request to draft",
        )

    async def close_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
    ) -> None:
        response = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/{pull_number}",
            json={"state": "closed"},
        )
        self._expect_success(response)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, str] | None = None,
    ) -> httpxyz.Response:
        for attempt in range(_DEFAULT_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                )
            except httpxyz.RequestError as error:
                raise GithubClientError(f"GitHub request failed: {error}") from error

            retry_after_seconds = self._retry_after_seconds(
                attempt=attempt,
                response=response,
            )
            if retry_after_seconds is None:
                return response

            logger.debug(
                "github rate limit encountered: method=%s path=%s status=%s attempt=%d "
                "retry_after_seconds=%.3f",
                method,
                path,
                response.status_code,
                attempt + 1,
                retry_after_seconds,
            )
            await asyncio.sleep(retry_after_seconds)

        raise AssertionError("Rate-limit retry loop did not return a response.")

    async def _get_paginated_json_array(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        response_name: str,
    ) -> tuple[object, ...]:
        items: list[object] = []
        next_path: str | None = path
        next_params = params

        while next_path is not None:
            response = await self._request(
                "GET",
                next_path,
                params=next_params,
            )
            payload = self._expect_success(response)
            if not isinstance(payload, list):
                raise GithubClientError(f"GitHub {response_name} response was not a JSON array.")
            items.extend(payload)
            next_path = response.links.get("next", {}).get("url")
            next_params = None

        return tuple(items)

    async def _graphql_query(
        self,
        query: str,
        *,
        response_name: str,
        variables: dict[str, object] | None = None,
    ) -> dict[str, object]:
        response = await self._request(
            "POST",
            "/graphql",
            json={
                "query": query,
                "variables": variables or {},
            },
        )
        payload = self._expect_success(response)
        if not isinstance(payload, dict):
            raise GithubClientError(f"GitHub {response_name} response was not a JSON object.")
        errors = payload.get("errors")
        if errors:
            raise GithubClientError(f"GitHub {response_name} failed: {errors}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GithubClientError(f"GitHub {response_name} response was missing `data`.")
        return data

    def _expect_success(self, response: httpxyz.Response) -> Any:
        try:
            response.raise_for_status()
        except httpxyz.HTTPStatusError as error:
            raise GithubClientError(
                f"GitHub request failed: {error.response.status_code} {error.response.text}",
                retry_after_seconds=_parse_retry_after_header(
                    error.response.headers.get("Retry-After")
                ),
                status_code=error.response.status_code,
            ) from error
        return response.json()

    def _expect_no_content(self, response: httpxyz.Response) -> None:
        try:
            response.raise_for_status()
        except httpxyz.HTTPStatusError as error:
            raise GithubClientError(
                f"GitHub request failed: {error.response.status_code} {error.response.text}",
                retry_after_seconds=_parse_retry_after_header(
                    error.response.headers.get("Retry-After")
                ),
                status_code=error.response.status_code,
            ) from error

    def _retry_after_seconds(
        self,
        *,
        attempt: int,
        response: httpxyz.Response,
    ) -> float | None:
        if not _is_retryable_rate_limit(response):
            return None
        if attempt >= _DEFAULT_RATE_LIMIT_RETRIES:
            return None

        retry_after_seconds = _parse_retry_after_header(response.headers.get("Retry-After"))
        if retry_after_seconds is not None:
            return retry_after_seconds

        reset_after_seconds = _seconds_until_rate_limit_reset(
            response.headers.get("X-RateLimit-Reset")
        )
        if reset_after_seconds is not None:
            return reset_after_seconds

        backoff_seconds = _DEFAULT_RATE_LIMIT_BACKOFF_SECONDS * (2**attempt)
        return min(backoff_seconds, _DEFAULT_MAX_RATE_LIMIT_BACKOFF_SECONDS)


def _is_retryable_rate_limit(response: httpxyz.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if "Retry-After" in response.headers or "X-RateLimit-Reset" in response.headers:
        return True
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    return "rate limit" in response.text.lower()


def _parse_retry_after_header(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        retry_after_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return max(retry_after_at.timestamp() - time.time(), 0.0)


def _seconds_until_rate_limit_reset(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value) - time.time(), 0.0)
    except ValueError:
        return None


def _graphql_repository_payload(
    payload: dict[str, object],
    *,
    response_name: str,
) -> dict[str, object]:
    repository = payload.get("repository")
    if repository is None:
        raise GithubClientError(f"GitHub {response_name} response was missing repository data.")
    if not isinstance(repository, dict):
        raise GithubClientError(f"GitHub {response_name} response had invalid repository data.")
    return repository


def _graphql_mutation_pull_request_payload(
    payload: dict[str, object],
    *,
    mutation_name: str,
    response_name: str,
) -> GithubPullRequest:
    result = payload.get(mutation_name)
    if not isinstance(result, dict):
        raise GithubClientError(f"GitHub {response_name} response was missing mutation data.")
    raw_pull_request = result.get("pullRequest")
    if raw_pull_request is None:
        raise GithubClientError(
            f"GitHub {response_name} response was missing a pull request payload."
        )
    return _validate_graphql_model(
        raw_pull_request,
        model=GithubPullRequest,
        error_message=f"GitHub {response_name} response had invalid mutation data.",
    )


def _chunked[ChunkValue](
    values: Sequence[ChunkValue],
    *,
    size: int,
) -> list[tuple[ChunkValue, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)]


def _pull_requests_by_number_query(numbers: Sequence[int]) -> str:
    selections = "\n".join(
        (
            f"      pr_{number}: pullRequest(number: {number}) {{\n"
            f"{_pull_request_graphql_selection(indent='        ')}\n"
            "      }"
        )
        for number in numbers
    )
    return (
        "query PullRequestsByNumber($owner: String!, $repo: String!) {\n"
        "  repository(owner: $owner, name: $repo) {\n"
        f"{selections}\n"
        "  }\n"
        "}\n"
    )


def _pull_requests_by_head_ref_query(aliases: dict[str, str]) -> str:
    selections = "\n".join(
        (
            f"    {alias}: pullRequests("
            f"first: 2, states: [OPEN, CLOSED], headRefName: {json.dumps(head_ref)}) {{\n"
            "      nodes {\n"
            f"{_pull_request_graphql_selection(indent='        ')}\n"
            "      }\n"
            "    }"
        )
        for alias, head_ref in aliases.items()
    )
    return (
        "query PullRequestsByHeadRef($owner: String!, $repo: String!) {\n"
        "  repository(owner: $owner, name: $repo) {\n"
        f"{selections}\n"
        "  }\n"
        "}\n"
    )


def _pull_request_review_decisions_query(numbers: Sequence[int]) -> str:
    selections = "\n".join(
        (
            f"      pr_{number}: pullRequest(number: {number}) {{\n"
            "        latestOpinionatedReviews(first: 100) {\n"
            "          nodes {\n"
            "            state\n"
            "            author {\n"
            "              login\n"
            "            }\n"
            "          }\n"
            "        }\n"
            "      }"
        )
        for number in numbers
    )
    return (
        "query PullRequestReviewDecisions($owner: String!, $repo: String!) {\n"
        "  repository(owner: $owner, name: $repo) {\n"
        f"{selections}\n"
        "  }\n"
        "}\n"
    )


def _pull_request_issue_comments_query(numbers: Sequence[int]) -> str:
    selections = "\n".join(
        (
            f"      pr_{number}: pullRequest(number: {number}) {{\n"
            "        comments(first: 100) {\n"
            "          nodes {\n"
            "            databaseId\n"
            "            body\n"
            "            url\n"
            "          }\n"
            "          pageInfo {\n"
            "            hasNextPage\n"
            "          }\n"
            "        }\n"
            "      }"
        )
        for number in numbers
    )
    return (
        "query PullRequestIssueComments($owner: String!, $repo: String!) {\n"
        "  repository(owner: $owner, name: $repo) {\n"
        f"{selections}\n"
        "  }\n"
        "}\n"
    )


def _mark_pull_request_ready_for_review_mutation() -> str:
    return (
        "mutation MarkPullRequestReadyForReview($pullRequestId: ID!) {\n"
        "  markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {\n"
        "    pullRequest {\n"
        f"{_pull_request_graphql_selection(indent='      ')}\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def _convert_pull_request_to_draft_mutation() -> str:
    return (
        "mutation ConvertPullRequestToDraft($pullRequestId: ID!) {\n"
        "  convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {\n"
        "    pullRequest {\n"
        f"{_pull_request_graphql_selection(indent='      ')}\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def _pull_request_graphql_selection(*, indent: str) -> str:
    return "\n".join(
        [
            f"{indent}id",
            f"{indent}number",
            f"{indent}state",
            f"{indent}isDraft",
            f"{indent}mergedAt",
            f"{indent}reviewDecision",
            f"{indent}url",
            f"{indent}title",
            f"{indent}body",
            f"{indent}baseRefName",
            f"{indent}headRefName",
            f"{indent}headRepositoryOwner {{",
            f"{indent}  login",
            f"{indent}}}",
        ]
    )


def _pull_request_connection_from_graphql(
    *,
    alias: str,
    connection: object,
    expected_head_label: str | None = None,
    response_name: str,
) -> tuple[GithubPullRequest, ...]:
    if connection is None:
        return ()
    parsed = _validate_graphql_model(
        connection,
        model=_GraphqlPullRequestConnection,
        error_message=(
            f"GitHub {response_name} response had invalid connection payload for {alias}."
        ),
    )
    if parsed.nodes is None:
        return ()
    pull_requests: list[GithubPullRequest] = []
    for raw_node in parsed.nodes:
        pull_request = _validate_graphql_model(
            raw_node,
            model=GithubPullRequest,
            error_message=(
                f"GitHub {response_name} response had invalid pull request payload for {alias}."
            ),
        )
        if expected_head_label is not None and pull_request.head.label != expected_head_label:
            continue
        pull_requests.append(pull_request)
    return tuple(pull_requests)


def build_github_client(*, base_url: str) -> GithubClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jj-review/dev",
    }
    if token := github_token_for_base_url(base_url):
        headers["Authorization"] = f"Bearer {token}"

    return GithubClient(
        httpxyz.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=30.0,
        )
    )


def _review_decision_from_graphql(
    *,
    alias: str,
    raw_pull_request: object,
    response_name: str,
) -> str | None:
    if raw_pull_request is None:
        return None
    if not isinstance(raw_pull_request, dict):
        raise GithubClientError(
            f"GitHub {response_name} response had invalid pull request payload for {alias}."
        )
    raw_latest_reviews = raw_pull_request.get("latestOpinionatedReviews")
    if raw_latest_reviews is None:
        return None
    parsed = _validate_graphql_model(
        raw_latest_reviews,
        model=_GraphqlReviewConnection,
        error_message=(
            f"GitHub {response_name} response had invalid latest reviews payload for {alias}."
        ),
    )
    if parsed.nodes is None:
        return None

    review_states: set[str] = set()
    for raw_node in parsed.nodes:
        review = _validate_graphql_model(
            raw_node,
            model=_GraphqlReview,
            error_message=(
                f"GitHub {response_name} response had invalid review payload for {alias}."
            ),
        )
        if review.author is None:
            continue
        normalized_state = review.state.upper()
        if normalized_state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        review_states.add(normalized_state)

    if "CHANGES_REQUESTED" in review_states:
        return "changes_requested"
    if "APPROVED" in review_states:
        return "approved"
    return None


def _issue_comments_from_graphql(
    *,
    alias: str,
    raw_pull_request: object,
    response_name: str,
) -> tuple[tuple[GithubIssueComment, ...], bool]:
    if raw_pull_request is None:
        return (), False
    if not isinstance(raw_pull_request, dict):
        raise GithubClientError(
            f"GitHub {response_name} response had invalid pull request payload for {alias}."
        )
    raw_comments = raw_pull_request.get("comments")
    if raw_comments is None:
        return (), False
    parsed = _validate_graphql_model(
        raw_comments,
        model=_GraphqlIssueCommentConnection,
        error_message=(
            f"GitHub {response_name} response had invalid comments payload for {alias}."
        ),
    )
    comments = tuple(
        _validate_graphql_model(
            comment,
            model=GithubIssueComment,
            error_message=(
                f"GitHub {response_name} response had invalid comment payload for {alias}."
            ),
        )
        for comment in parsed.nodes or ()
        if comment is not None
    )
    has_next_page = (
        parsed.page_info is not None and parsed.page_info.get("hasNextPage") is True
    )
    return comments, has_next_page


def _validate_graphql_model[GraphqlModel: BaseModel](
    payload: object,
    *,
    model: type[GraphqlModel],
    error_message: str,
) -> GraphqlModel:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise GithubClientError(error_message) from error
