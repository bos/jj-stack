"""Minimal async GitHub API client."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from email.utils import parsedate_to_datetime
from textwrap import dedent, indent

import httpxyz
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jj_stack.errors import EXIT_GITHUB, SummarizedError
from jj_stack.github.auth import github_token, github_token_from_env
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import (
    GithubIssueComment,
    GithubPR,
    GithubPRReview,
    GithubRepo,
    GithubStack,
    GithubStackMerge,
    GithubStackMergeSubmission,
)

logger = logging.getLogger(__name__)
GITHUB_API_BASE_URL = "https://api.github.com"
_GRAPHQL_PR_BATCH_SIZE = 25

_DEFAULT_RATE_LIMIT_RETRIES = 3
_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 1.0
_DEFAULT_MAX_RATE_LIMIT_BACKOFF_SECONDS = 8.0


class GithubClientError(SummarizedError):
    """Raised when GitHub returns a non-success response."""

    exit_code = EXIT_GITHUB

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

    def detail(self) -> str:
        """Return the transport detail with known request prefixes stripped."""

        message = str(self).strip()
        for prefix in (
            "GitHub request failed: ",
            "GitHub pull request base lookup failed: ",
            "GitHub pull request head lookup failed: ",
            "GitHub pull request batch lookup failed: ",
            "GitHub pull request review decision lookup failed: ",
            "GitHub issue comment list failed: ",
        ):
            if message.startswith(prefix):
                return message.removeprefix(prefix).strip()
        return message

    def is_repo_not_found(self) -> bool:
        """Whether the error indicates the repo is missing or inaccessible."""

        if "Could not resolve to a Repository with the name" in self.detail():
            return True
        return self.status_code == 404

    def request_failure_detail(self) -> str:
        """Return the status code if known, otherwise the transport detail."""

        if self.status_code is None:
            return self.detail()
        return f"GitHub {self.status_code}"

    def user_facing_reason(self) -> str:
        """Render a concise failure reason suitable after an action prefix."""

        if self.status_code == 401:
            return "auth failed - check GITHUB_TOKEN"
        if self.status_code == 403:
            return "access denied - check GITHUB_TOKEN and repo access"
        if self.is_repo_not_found():
            message = "repo not found or inaccessible"
            if github_token_from_env() is None:
                return f"{message} - check GITHUB_TOKEN or gh auth"
            return message
        return f"request failed ({self.request_failure_detail()})"


class _GraphqlPRConnection(BaseModel):
    nodes: tuple[GithubPR, ...]


class _GraphqlPageInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    has_next_page: bool = Field(default=False, alias="hasNextPage")


class _GraphqlIssueCommentConnection(BaseModel):
    nodes: tuple[GithubIssueComment | None, ...] | None = None
    page_info: _GraphqlPageInfo | None = Field(default=None, alias="pageInfo")


class _GraphqlIssueCommentsPR(BaseModel):
    comments: _GraphqlIssueCommentConnection | None = None


class GithubClient:
    """Thin async wrapper around the GitHub API, bound to one repo."""

    def __init__(self, client: httpxyz.AsyncClient, *, repo: GithubRepoAddress) -> None:
        self._client = client
        self._repo = repo
        self._repo_path = f"/repos/{repo.owner}/{repo.repo}"
        self._repo_variables: dict[str, object] = {
            "owner": repo.owner,
            "repo": repo.repo,
        }

    @property
    def repo(self) -> GithubRepoAddress:
        """The GitHub repo every request targets."""

        return self._repo

    async def __aenter__(self) -> GithubClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_repo(self) -> GithubRepo:
        response = await self._request("GET", self._repo_path)
        return GithubRepo.model_validate(self._expect_success(response))

    async def list_branches_for_head_commit(self, *, commit_sha: str) -> tuple[str, ...]:
        """List branches whose head is exactly the given commit."""

        response = await self._request(
            "GET",
            f"{self._repo_path}/commits/{commit_sha}/branches-where-head",
        )
        if response.status_code == 422:
            try:
                error_payload = response.json()
            except json.JSONDecodeError:
                error_payload = None
            if isinstance(error_payload, dict) and str(
                error_payload.get("message", "")
            ).startswith("No commit found for SHA:"):
                return ()
        payload = self._expect_json_payload(response, response_name="branch-at-commit lookup")
        if not isinstance(payload, list) or any(
            not isinstance(branch, dict) or not isinstance(branch.get("name"), str)
            for branch in payload
        ):
            raise GithubClientError(
                "GitHub branch-at-commit lookup response had invalid branch data."
            )
        return tuple(branch["name"] for branch in payload)

    async def list_stacks(self) -> tuple[GithubStack, ...]:
        payload = await self._get_paginated_json_array(
            f"{self._repo_path}/stacks",
            response_name="stack list",
        )
        return tuple(
            _validate_stack_payload(item, response_name="stack list") for item in payload
        )

    async def get_stack(self, *, stack_number: int) -> GithubStack:
        response = await self._request("GET", f"{self._repo_path}/stacks/{stack_number}")
        return _validate_stack_payload(
            self._expect_json_payload(response, response_name="stack lookup"),
            response_name="stack lookup",
        )

    async def create_stack(self, *, pr_numbers: Sequence[int]) -> GithubStack:
        response = await self._request(
            "POST",
            f"{self._repo_path}/stacks",
            json={"pull_requests": list(pr_numbers)},
        )
        return _validate_stack_payload(
            self._expect_json_payload(response, response_name="stack creation"),
            response_name="stack creation",
        )

    async def append_to_stack(
        self,
        *,
        stack_number: int,
        pr_numbers: Sequence[int],
    ) -> GithubStack:
        response = await self._request(
            "POST",
            f"{self._repo_path}/stacks/{stack_number}/add",
            json={"pull_requests": list(pr_numbers)},
        )
        return _validate_stack_payload(
            self._expect_json_payload(response, response_name="stack append"),
            response_name="stack append",
        )

    async def unstack(self, *, stack_number: int) -> GithubStack | None:
        response = await self._request(
            "POST",
            f"{self._repo_path}/stacks/{stack_number}/unstack",
        )
        if response.status_code == 204:
            self._expect_no_content(response)
            return None
        return _validate_stack_payload(
            self._expect_json_payload(response, response_name="unstack"),
            response_name="unstack",
        )

    async def get_pr(
        self,
        *,
        pr_number: int,
    ) -> GithubPR:
        response = await self._request(
            "GET",
            f"{self._repo_path}/pulls/{pr_number}",
        )
        return GithubPR.model_validate(self._expect_success(response))

    async def get_prs_by_numbers(
        self,
        *,
        pr_numbers: Sequence[int],
    ) -> dict[int, GithubPR | None]:
        numbers = sorted(set(pr_numbers))
        if not numbers:
            return {}

        results: dict[int, GithubPR | None] = {}
        for chunk in _chunked(numbers, size=_GRAPHQL_PR_BATCH_SIZE):
            query = _prs_by_number_query(chunk)
            payload = await self._graphql_query(
                query,
                variables=self._repo_variables,
                response_name="pull request batch lookup",
            )
            repo = _graphql_repo_payload(
                payload,
                response_name="pull request batch lookup",
            )
            for number in chunk:
                alias = f"pr_{number}"
                raw_pr = repo.get(alias)
                if raw_pr is None:
                    results[number] = None
                    continue
                results[number] = _validate_graphql_model(
                    raw_pr,
                    model=GithubPR,
                    error_message=(
                        "GitHub pull request batch lookup response had invalid pull request "
                        f"payload for #{number}."
                    ),
                )
        return results

    async def get_open_prs_by_head_refs(
        self,
        *,
        head_refs: Sequence[str],
    ) -> dict[str, tuple[GithubPR, ...]]:
        return await self._get_open_prs_by_refs(refs=head_refs, base=False)

    async def get_open_prs_by_base_refs(
        self,
        *,
        base_refs: Sequence[str],
    ) -> dict[str, tuple[GithubPR, ...]]:
        return await self._get_open_prs_by_refs(refs=base_refs, base=True)

    async def _get_open_prs_by_refs(
        self,
        *,
        base: bool,
        refs: Sequence[str],
    ) -> dict[str, tuple[GithubPR, ...]]:
        refs = sorted(set(refs))
        if not refs:
            return {}

        kind = "base" if base else "head"
        response_name = f"pull request {kind} lookup"
        results: dict[str, tuple[GithubPR, ...]] = {}
        for chunk in _chunked(refs, size=_GRAPHQL_PR_BATCH_SIZE):
            aliases = {f"{kind}_{index}": ref for index, ref in enumerate(chunk)}
            query = _open_prs_by_ref_query(aliases, base=base)
            payload = await self._graphql_query(
                query,
                variables=self._repo_variables,
                response_name=response_name,
            )
            repo = _graphql_repo_payload(
                payload,
                response_name=response_name,
            )
            for alias, ref in aliases.items():
                results[ref] = _pr_connection_from_graphql(
                    alias=alias,
                    connection=repo.get(alias),
                    expected_head_label=(None if base else f"{self._repo.owner}:{ref}"),
                    response_name=response_name,
                )
        return results

    async def create_pr(
        self,
        *,
        base: str,
        body: str,
        draft: bool = False,
        head: str,
        title: str,
    ) -> GithubPR:
        response = await self._request(
            "POST",
            f"{self._repo_path}/pulls",
            json={
                "base": base,
                "body": body,
                "draft": draft,
                "head": head,
                "title": title,
            },
        )
        return GithubPR.model_validate(self._expect_success(response))

    async def list_pr_reviews(
        self,
        *,
        pr_number: int,
    ) -> tuple[GithubPRReview, ...]:
        payload = await self._get_paginated_json_array(
            f"{self._repo_path}/pulls/{pr_number}/reviews",
            response_name="pull request reviews",
        )
        return tuple(GithubPRReview.model_validate(item) for item in payload)

    async def list_issue_comments(
        self,
        *,
        issue_number: int,
    ) -> tuple[GithubIssueComment, ...]:
        payload = await self._get_paginated_json_array(
            f"{self._repo_path}/issues/{issue_number}/comments",
            response_name="issue comment list",
        )
        return tuple(GithubIssueComment.model_validate(item) for item in payload)

    async def get_issue_comments_by_pr_numbers(
        self,
        *,
        pr_numbers: Sequence[int],
    ) -> dict[int, tuple[GithubIssueComment, ...]]:
        numbers = sorted(set(pr_numbers))
        if not numbers:
            return {}

        results: dict[int, tuple[GithubIssueComment, ...]] = {}
        fallback_numbers: list[int] = []
        for chunk in _chunked(numbers, size=_GRAPHQL_PR_BATCH_SIZE):
            query = _pr_issue_comments_query(chunk)
            payload = await self._graphql_query(
                query,
                variables=self._repo_variables,
                response_name="pull request issue comment lookup",
            )
            repo = _graphql_repo_payload(
                payload,
                response_name="pull request issue comment lookup",
            )
            for number in chunk:
                alias = f"pr_{number}"
                comments, has_next_page = _issue_comments_from_graphql(
                    alias=alias,
                    raw_pr=repo.get(alias),
                    response_name="pull request issue comment lookup",
                )
                if has_next_page:
                    fallback_numbers.append(number)
                    continue
                results[number] = comments

        for number in fallback_numbers:
            results[number] = await self.list_issue_comments(issue_number=number)
        return results

    async def create_issue_comment(
        self,
        *,
        issue_number: int,
        body: str,
    ) -> GithubIssueComment:
        response = await self._request(
            "POST",
            f"{self._repo_path}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return GithubIssueComment.model_validate(self._expect_success(response))

    async def update_issue_comment(
        self,
        *,
        comment_id: int,
        body: str,
    ) -> GithubIssueComment:
        response = await self._request(
            "PATCH",
            f"{self._repo_path}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return GithubIssueComment.model_validate(self._expect_success(response))

    async def delete_issue_comment(
        self,
        *,
        comment_id: int,
    ) -> None:
        response = await self._request(
            "DELETE",
            f"{self._repo_path}/issues/comments/{comment_id}",
        )
        self._expect_no_content(response)

    async def request_reviewers(
        self,
        *,
        pr_number: int,
        reviewers: list[str],
        team_reviewers: list[str],
    ) -> None:
        response = await self._request(
            "POST",
            f"{self._repo_path}/pulls/{pr_number}/requested_reviewers",
            json={"reviewers": reviewers, "team_reviewers": team_reviewers},
        )
        self._expect_success(response)

    async def add_labels(
        self,
        *,
        issue_number: int,
        labels: list[str],
    ) -> None:
        response = await self._request(
            "POST",
            f"{self._repo_path}/issues/{issue_number}/labels",
            json={"labels": labels},
        )
        self._expect_success(response)

    async def update_pr(
        self,
        *,
        pr_number: int,
        base: str | None = None,
        body: str | None = None,
        title: str | None = None,
    ) -> GithubPR:
        fields = {"base": base, "body": body, "title": title}
        response = await self._request(
            "PATCH",
            f"{self._repo_path}/pulls/{pr_number}",
            json={name: value for name, value in fields.items() if value is not None},
        )
        return GithubPR.model_validate(self._expect_success(response))

    async def mark_pr_ready_for_review(
        self,
        *,
        pr_id: str,
    ) -> GithubPR:
        payload = await self._graphql_query(
            _mark_pr_ready_for_review_mutation(),
            response_name="mark pull request ready for review",
            variables={"pullRequestId": pr_id},
        )
        return _graphql_mutation_pr_payload(
            payload,
            mutation_name="markPullRequestReadyForReview",
            response_name="mark pull request ready for review",
        )

    async def convert_pr_to_draft(
        self,
        *,
        pr_id: str,
    ) -> GithubPR:
        payload = await self._graphql_query(
            _convert_pr_to_draft_mutation(),
            response_name="convert pull request to draft",
            variables={"pullRequestId": pr_id},
        )
        return _graphql_mutation_pr_payload(
            payload,
            mutation_name="convertPullRequestToDraft",
            response_name="convert pull request to draft",
        )

    async def base_branch_uses_merge_queue(self, *, branch: str) -> bool:
        payload = await self._graphql_query(
            _base_branch_merge_queue_query(),
            response_name="base branch merge queue lookup",
            variables={
                **self._repo_variables,
                "branch": branch,
                "qualified": f"refs/heads/{branch}",
            },
        )
        repo = _graphql_repo_payload(
            payload,
            response_name="base branch merge queue lookup",
        )
        if repo.get("mergeQueue") is not None:
            return True
        ref = repo.get("ref")
        rules = ref.get("rules") if isinstance(ref, dict) else None
        nodes = rules.get("nodes") if isinstance(rules, dict) else None
        return isinstance(nodes, list) and any(
            isinstance(node, dict) and node.get("type") == "MERGE_QUEUE" for node in nodes
        )

    async def submit_stack_merge(
        self,
        *,
        expected_head_sha: str,
        merge_action: str,
        merge_method: str | None,
        pr_number: int,
    ) -> GithubStackMergeSubmission:
        body: dict[str, object] = {
            "merge_action": merge_action,
            "sha": expected_head_sha,
        }
        if merge_method is not None:
            body["merge_method"] = merge_method
        response = await self._request(
            "PUT",
            f"{self._repo_path}/pulls/{pr_number}/merge-async",
            json=body,
        )
        # 409 means GitHub already has an operation in flight for this pull request, not that
        # the merge conflicts.
        already_pending = response.status_code == 409
        if already_pending:
            try:
                payload = response.json()
            except json.JSONDecodeError as error:
                raise GithubClientError(
                    "GitHub's already-pending merge response was not valid JSON.",
                    status_code=409,
                ) from error
        else:
            payload = self._expect_success(response)
        return GithubStackMergeSubmission(
            already_pending=already_pending,
            result=_validate_stack_merge_payload(payload),
        )

    async def poll_stack_merge(
        self,
        *,
        operation_uuid: str,
        pr_number: int,
    ) -> GithubStackMerge:
        response = await self._request(
            "GET",
            f"{self._repo_path}/pulls/{pr_number}/merge-async/{operation_uuid}",
        )
        return _validate_stack_merge_payload(self._expect_success(response))

    async def close_pr(
        self,
        *,
        pr_number: int,
    ) -> None:
        response = await self._request(
            "PATCH",
            f"{self._repo_path}/issues/{pr_number}",
            json={"state": "closed"},
        )
        self._expect_success(response)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
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
        response_name: str,
    ) -> tuple[object, ...]:
        items: list[object] = []
        next_path: str | None = path

        while next_path is not None:
            response = await self._request("GET", next_path)
            payload = self._expect_json_payload(response, response_name=response_name)
            if not isinstance(payload, list):
                raise GithubClientError(f"GitHub {response_name} response was not a JSON array.")
            items.extend(payload)
            next_path = response.links.get("next", {}).get("url")

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

    def _expect_success(self, response: httpxyz.Response) -> object:
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

    def _expect_json_payload(
        self,
        response: httpxyz.Response,
        *,
        response_name: str,
    ) -> object:
        try:
            return self._expect_success(response)
        except json.JSONDecodeError as error:
            raise GithubClientError(
                f"GitHub {response_name} response was not valid JSON."
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
    except TypeError, ValueError, IndexError:
        return None
    return max(retry_after_at.timestamp() - time.time(), 0.0)


def _seconds_until_rate_limit_reset(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value) - time.time(), 0.0)
    except ValueError:
        return None


def _graphql_repo_payload(
    payload: dict[str, object],
    *,
    response_name: str,
) -> dict[str, object]:
    repo = payload.get("repository")
    if repo is None:
        raise GithubClientError(f"GitHub {response_name} response was missing repo data.")
    if not isinstance(repo, dict):
        raise GithubClientError(f"GitHub {response_name} response had invalid repo data.")
    return repo


def _graphql_mutation_pr_payload(
    payload: dict[str, object],
    *,
    mutation_name: str,
    response_name: str,
) -> GithubPR:
    result = payload.get(mutation_name)
    if not isinstance(result, dict):
        raise GithubClientError(f"GitHub {response_name} response was missing mutation data.")
    raw_pr = result.get("pullRequest")
    if raw_pr is None:
        raise GithubClientError(
            f"GitHub {response_name} response was missing a pull request payload."
        )
    return _validate_graphql_model(
        raw_pr,
        model=GithubPR,
        error_message=f"GitHub {response_name} response had invalid mutation data.",
    )


def _chunked[ChunkValue](
    values: Sequence[ChunkValue],
    *,
    size: int,
) -> list[tuple[ChunkValue, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)]


def _prs_by_number_query(numbers: Sequence[int]) -> str:
    selections = "\n\n".join(
        _graphql_document(
            f"""
            pr_{number}: pullRequest(number: {number}) {{
              ...PullRequestFields
            }}
            """
        ).strip()
        for number in numbers
    )
    return _with_pr_fields_fragment(
        _repo_graphql_query(
            operation_name="PullRequestsByNumber",
            selections=selections,
        )
    )


def _open_prs_by_ref_query(aliases: dict[str, str], *, base: bool) -> str:
    first = 100 if base else 2
    operation_name = "OpenPullRequestsByBaseRef" if base else "OpenPullRequestsByHeadRef"
    ref_argument = "baseRefName" if base else "headRefName"
    selections = "\n\n".join(
        _graphql_document(
            f"""
            {alias}: pullRequests(
              first: {first},
              states: [OPEN],
              {ref_argument}: {json.dumps(ref)}
            ) {{
              nodes {{
                ...PullRequestFields
              }}
            }}
            """
        ).strip()
        for alias, ref in aliases.items()
    )
    return _with_pr_fields_fragment(
        _repo_graphql_query(
            operation_name=operation_name,
            selections=selections,
        )
    )


def _pr_issue_comments_query(numbers: Sequence[int]) -> str:
    selections = "\n\n".join(
        _graphql_document(
            f"""
            pr_{number}: pullRequest(number: {number}) {{
              comments(first: 100) {{
                nodes {{
                  databaseId
                  body
                }}
                pageInfo {{
                  hasNextPage
                }}
              }}
            }}
            """
        ).strip()
        for number in numbers
    )
    return _repo_graphql_query(
        operation_name="PullRequestIssueComments",
        selections=selections,
    )


def _mark_pr_ready_for_review_mutation() -> str:
    return _with_pr_fields_fragment(
        _graphql_document(
            """
            mutation MarkPullRequestReadyForReview($pullRequestId: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                pullRequest {
                  ...PullRequestFields
                }
              }
            }
            """
        )
    )


def _convert_pr_to_draft_mutation() -> str:
    return _with_pr_fields_fragment(
        _graphql_document(
            """
            mutation ConvertPullRequestToDraft($pullRequestId: ID!) {
              convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {
                pullRequest {
                  ...PullRequestFields
                }
              }
            }
            """
        )
    )


def _pr_fields_fragment() -> str:
    return _graphql_document(
        """
        fragment PullRequestFields on PullRequest {
          id
          number
          state
          isDraft
          mergeQueueEntry {
            id
          }
          mergeCommit {
            oid
          }
          mergedAt
          reviewDecision
          url
          title
          body
          baseRefName
          headRefName
          headRefOid
          headRepositoryOwner {
            login
          }
        }
        """
    )


def _base_branch_merge_queue_query() -> str:
    return _graphql_document(
        """
        query BaseBranchMergeQueue(
          $owner: String!,
          $repo: String!,
          $branch: String!,
          $qualified: String!
        ) {
          repository(owner: $owner, name: $repo) {
            mergeQueue(branch: $branch) {
              id
            }
            ref(qualifiedName: $qualified) {
              rules(first: 50) {
                nodes {
                  type
                }
              }
            }
          }
        }
        """
    )


def _repo_graphql_query(*, operation_name: str, selections: str) -> str:
    return "\n".join(
        [
            f"query {operation_name}($owner: String!, $repo: String!) {{",
            "  repository(owner: $owner, name: $repo) {",
            indent(selections.rstrip(), "    "),
            "  }",
            "}",
            "",
        ]
    )


def _with_pr_fields_fragment(document: str) -> str:
    return f"{document.rstrip()}\n\n{_pr_fields_fragment()}"


def _graphql_document(document: str) -> str:
    return dedent(document).strip() + "\n"


def _pr_connection_from_graphql(
    *,
    alias: str,
    connection: object,
    expected_head_label: str | None = None,
    response_name: str,
) -> tuple[GithubPR, ...]:
    parsed = _validate_graphql_model(
        connection,
        model=_GraphqlPRConnection,
        error_message=(
            f"GitHub {response_name} response had invalid connection payload for {alias}."
        ),
    )
    prs: list[GithubPR] = []
    for pr in parsed.nodes:
        if expected_head_label is not None and pr.head.label != expected_head_label:
            continue
        prs.append(pr)
    return tuple(prs)


def build_github_client(*, repo: GithubRepoAddress) -> GithubClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jj-stack/dev",
    }
    if token := github_token():
        headers["Authorization"] = f"Bearer {token}"

    return GithubClient(
        httpxyz.AsyncClient(
            base_url=GITHUB_API_BASE_URL,
            headers=headers,
            timeout=30.0,
        ),
        repo=repo,
    )


def _issue_comments_from_graphql(
    *,
    alias: str,
    raw_pr: object,
    response_name: str,
) -> tuple[tuple[GithubIssueComment, ...], bool]:
    if raw_pr is None:
        return (), False
    parsed = _validate_graphql_model(
        raw_pr,
        model=_GraphqlIssueCommentsPR,
        error_message=(
            f"GitHub {response_name} response had invalid pull request payload for {alias}."
        ),
    )
    comments = parsed.comments
    if comments is None:
        return (), False
    valid_comments = tuple(comment for comment in comments.nodes or () if comment is not None)
    has_next_page = comments.page_info is not None and comments.page_info.has_next_page
    return valid_comments, has_next_page


def _validate_stack_payload(payload: object, *, response_name: str) -> GithubStack:
    try:
        return GithubStack.model_validate(payload)
    except ValidationError as error:
        raise GithubClientError(
            f"GitHub {response_name} response had invalid stack data."
        ) from error


def _validate_stack_merge_payload(payload: object) -> GithubStackMerge:
    try:
        return GithubStackMerge.model_validate(payload)
    except ValidationError as error:
        raise GithubClientError("GitHub stack merge response had invalid data.") from error


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
