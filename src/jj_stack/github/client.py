"""Minimal async GitHub API client."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from email.utils import parsedate_to_datetime
from itertools import batched
from math import ceil
from textwrap import dedent, indent, shorten
from typing import Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jj_stack.errors import EXIT_GITHUB, SummarizedError
from jj_stack.github.auth import github_token
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import CommitId
from jj_stack.models.github import (
    GithubIssueComment,
    GithubPR,
    GithubPRReview,
    GithubPRRevision,
    GithubRepo,
    GithubStack,
    GithubStackMerge,
    GithubStackMergeSubmission,
)

logger = logging.getLogger(__name__)
GITHUB_API_BASE_URL = "https://api.github.com"

type RateLimitKind = Literal["primary", "secondary"]
_GRAPHQL_PR_BATCH_SIZE = 25

REPO_NOT_FOUND_REASON = "repo not found or inaccessible - check GITHUB_TOKEN or gh auth"
_DEFAULT_RATE_LIMIT_RETRIES = 3
_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 1.0
_MAX_RATE_LIMIT_WAIT_SECONDS = 60.0
_RATE_LIMIT_NOTICE_SECONDS = 5.0


class GraphqlError(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str
    type: str | None = None
    path: tuple[str | int, ...] = ()


class _GraphqlResponse(BaseModel):
    data: dict[str, object] | None = None
    errors: tuple[GraphqlError, ...] = ()


class _RestErrorDetail(BaseModel):
    message: str = ""


class _RestErrorBody(BaseModel):
    message: str
    errors: tuple[_RestErrorDetail, ...] = ()


class GithubClientError(SummarizedError):
    """Raised when a GitHub request fails or returns an unusable response."""

    exit_code = EXIT_GITHUB

    def __init__(
        self,
        message: str,
        *,
        body: str = "",
        graphql_errors: tuple[GraphqlError, ...] = (),
        rate_limit: RateLimitKind | None = None,
        rate_limit_reset_seconds: float | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.body = body
        self.graphql_errors = graphql_errors
        self.rate_limit = rate_limit
        self.rate_limit_reset_seconds = rate_limit_reset_seconds
        self.status_code = status_code

    def is_repo_not_found(self) -> bool:
        """Whether GitHub reported the repository itself as unresolvable."""

        return any(
            error.type == "NOT_FOUND" and error.path == ("repository",)
            for error in self.graphql_errors
        )

    def github_message(self) -> str:
        """Return GitHub's own explanation from the JSON body, bounded in length."""

        try:
            body = _RestErrorBody.model_validate_json(self.body)
        except ValidationError:
            return ""
        reasons = (body.message, *(error.message for error in body.errors))
        quoted = ": ".join(reason for reason in reasons if reason.strip())
        # The body is remote input on its way to a terminal, so drop anything unprintable
        # rather than forwarding an escape sequence.
        printable = "".join(character if character.isprintable() else " " for character in quoted)
        shortened = shorten(printable, width=200, placeholder=" ...")
        # A single oversized token shortens to nothing but the placeholder, which says less
        # than the bare status does.
        return "" if shortened.strip() == "..." else shortened

    def request_failure_detail(self) -> str:
        """Return the status and GitHub's explanation if known, otherwise the message."""

        if self.graphql_errors:
            return "; ".join(error.message for error in self.graphql_errors)
        if self.status_code is None:
            return str(self).strip()
        if reason := self.github_message():
            return f"GitHub {self.status_code}: {reason}"
        return f"GitHub {self.status_code}"

    def user_facing_reason(self) -> str:
        """Render a concise failure reason suitable after an action prefix."""

        if self.status_code == 401:
            return "auth failed - check GITHUB_TOKEN"
        if self.status_code == 403:
            # GitHub refuses a rate-limited request with the same status as a token problem,
            # and the retries give up long before a primary limit resets.
            if self.rate_limit is not None:
                reset = self.rate_limit_reset_seconds
                minutes = None if reset is None else max(1, ceil(reset / 60))
                resets = "" if minutes is None else f", resets in about {minutes} min"
                return f"GitHub {self.rate_limit} rate limit reached{resets} - rerun later"
            return "access denied - check GITHUB_TOKEN and repo access"
        if self.is_repo_not_found():
            return REPO_NOT_FOUND_REASON
        return f"request failed ({self.request_failure_detail()})"


class _GraphqlPRConnection(BaseModel):
    nodes: tuple[GithubPR, ...]


class _GraphqlPageInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    end_cursor: str | None = Field(default=None, alias="endCursor")
    has_next_page: bool = Field(default=False, alias="hasNextPage")


class _GraphqlGitObject(BaseModel):
    oid: CommitId


class _GraphqlRef(BaseModel):
    name: str
    prefix: str
    target: _GraphqlGitObject


class _GraphqlRefConnection(BaseModel):
    nodes: tuple[_GraphqlRef | None, ...]
    page_info: _GraphqlPageInfo = Field(alias="pageInfo")


class _GraphqlIssueCommentConnection(BaseModel):
    nodes: tuple[GithubIssueComment | None, ...] | None = None
    page_info: _GraphqlPageInfo = Field(alias="pageInfo")


class _GraphqlForcePushEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    after_commit: _GraphqlGitObject | None = Field(default=None, alias="afterCommit")
    before_commit: _GraphqlGitObject | None = Field(default=None, alias="beforeCommit")


class _GraphqlTimelineItemConnection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    filtered_count: int = Field(alias="filteredCount")
    nodes: tuple[_GraphqlForcePushEvent | None, ...] | None = None


class _GraphqlPRHistory(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    comments: _GraphqlIssueCommentConnection | None = None
    timeline_items: _GraphqlTimelineItemConnection | None = Field(
        default=None,
        alias="timelineItems",
    )


class GithubClient:
    """Thin async wrapper around the GitHub API, bound to one repo."""

    def __init__(self, client: httpx2.AsyncClient, *, repo: GithubRepoAddress) -> None:
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
        return _validate_model(
            self._expect_json_payload(response, response_name="repo lookup"),
            model=GithubRepo,
            error_context="GitHub repo lookup response had invalid data",
        )

    async def get_branch_targets(
        self,
        *,
        branches: Sequence[str],
    ) -> dict[str, CommitId]:
        """Return exact GitHub branch targets without advertising unrelated refs."""

        ordered = tuple(dict.fromkeys(branches))
        targets: dict[str, CommitId] = {}
        for chunk in batched(ordered, _GRAPHQL_PR_BATCH_SIZE, strict=False):
            query, branch_variables = _branch_targets_query(chunk)
            payload = await self._graphql_query(
                query,
                variables={**self._repo_variables, **branch_variables},
                response_name="branch target lookup",
            )
            repo = _graphql_repo_payload(payload, response_name="branch target lookup")
            for index, branch in enumerate(chunk):
                raw_ref = repo.get(f"branch_{index}")
                if raw_ref is None:
                    continue
                observed_branch, target = _branch_target_from_graphql(
                    raw_ref,
                    response_name="branch target lookup",
                )
                if observed_branch != branch:
                    raise GithubClientError(
                        "GitHub branch target lookup returned a different branch."
                    )
                targets[branch] = target
        return targets

    async def find_branch_targets_by_suffix(
        self,
        *,
        branch_prefix: str,
        suffixes: Sequence[str],
    ) -> dict[str, CommitId]:
        """Find branch targets under one namespace by exact name suffix."""

        ordered = tuple(dict.fromkeys(suffixes))
        targets: dict[str, CommitId] = {}
        for chunk in batched(ordered, _GRAPHQL_PR_BATCH_SIZE, strict=False):
            pending: tuple[tuple[str, str | None], ...] = tuple(
                (suffix, None) for suffix in chunk
            )
            while pending:
                query, suffix_variables = _branch_targets_by_suffix_query(
                    after_cursors=tuple(cursor for _suffix, cursor in pending),
                    branch_prefix=branch_prefix,
                    suffixes=tuple(suffix for suffix, _cursor in pending),
                )
                payload = await self._graphql_query(
                    query,
                    variables={**self._repo_variables, **suffix_variables},
                    response_name="branch suffix lookup",
                )
                repo = _graphql_repo_payload(payload, response_name="branch suffix lookup")
                next_page: list[tuple[str, str]] = []
                for index, (suffix, _cursor) in enumerate(pending):
                    connection = _validate_model(
                        repo.get(f"suffix_{index}"),
                        model=_GraphqlRefConnection,
                        error_context=(
                            "GitHub branch suffix lookup response had invalid ref data"
                        ),
                    )
                    for raw_ref in connection.nodes:
                        if raw_ref is None:
                            continue
                        branch, target = _branch_target(raw_ref)
                        if branch.startswith(branch_prefix) and branch.endswith(suffix):
                            targets[branch] = target
                    if connection.page_info.has_next_page:
                        cursor = connection.page_info.end_cursor
                        if cursor is None:
                            raise GithubClientError(
                                "GitHub branch suffix lookup response had no page cursor."
                            )
                        next_page.append((suffix, cursor))
                pending = tuple(next_page)
        return targets

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
            _expect_success(response)
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
        return _validate_model(
            self._expect_json_payload(response, response_name="pull request lookup"),
            model=GithubPR,
            error_context="GitHub pull request lookup response had invalid data",
        )

    async def get_prs_by_numbers(
        self,
        *,
        pr_numbers: Sequence[int],
    ) -> dict[int, GithubPR | None]:
        numbers = sorted(set(pr_numbers))
        if not numbers:
            return {}

        results: dict[int, GithubPR | None] = {}
        for chunk in batched(numbers, _GRAPHQL_PR_BATCH_SIZE, strict=False):
            query = _prs_by_number_query(chunk)
            payload = await self._graphql_query(
                query,
                response_name="pull request batch lookup",
                tolerate_missing_selections=True,
                variables=self._repo_variables,
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
                results[number] = _validate_model(
                    raw_pr,
                    model=GithubPR,
                    error_context=(
                        "GitHub pull request batch lookup response had invalid pull request "
                        f"payload for #{number}"
                    ),
                )
        return results

    async def get_open_prs_by_head_refs(
        self,
        *,
        head_refs: Sequence[str],
    ) -> dict[str, tuple[GithubPR, ...]]:
        return await self._get_prs_by_refs(refs=head_refs, base=False)

    async def get_prs_by_base_refs(
        self,
        *,
        base_refs: Sequence[str],
    ) -> dict[str, tuple[GithubPR, ...]]:
        return await self._get_prs_by_refs(refs=base_refs, base=True)

    async def _get_prs_by_refs(
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
        for chunk in batched(refs, _GRAPHQL_PR_BATCH_SIZE, strict=False):
            aliases = {f"{kind}_{index}": ref for index, ref in enumerate(chunk)}
            query, ref_variables = _prs_by_ref_query(aliases, base=base)
            payload = await self._graphql_query(
                query,
                variables={**self._repo_variables, **ref_variables},
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
        return _validate_model(
            self._expect_json_payload(response, response_name="pull request creation"),
            model=GithubPR,
            error_context="GitHub pull request creation response had invalid data",
        )

    async def list_pr_reviews(
        self,
        *,
        pr_number: int,
    ) -> tuple[GithubPRReview, ...]:
        payload = await self._get_paginated_json_array(
            f"{self._repo_path}/pulls/{pr_number}/reviews",
            response_name="pull request reviews",
        )
        return tuple(
            _validate_model(
                item,
                model=GithubPRReview,
                error_context="GitHub pull request reviews response had invalid data",
            )
            for item in payload
        )

    async def find_issue_comments_by_body_marker(
        self,
        *,
        body_marker: str,
        pr_numbers: Sequence[int],
    ) -> dict[int, GithubIssueComment | None]:
        comments_by_marker, _revisions = await self._get_pr_history(
            body_markers=(body_marker,),
            pr_numbers=pr_numbers,
            revision_limit=None,
        )
        return comments_by_marker[body_marker]

    async def find_issue_comments_and_revisions(
        self,
        *,
        body_markers: Sequence[str],
        pr_numbers: Sequence[int],
        revision_limit: int,
    ) -> tuple[
        dict[str, dict[int, GithubIssueComment | None]],
        dict[int, tuple[GithubPRRevision, ...]],
    ]:
        """Batch managed-comment lookups with recent PR revisions."""

        return await self._get_pr_history(
            body_markers=body_markers,
            pr_numbers=pr_numbers,
            revision_limit=revision_limit,
        )

    async def _get_pr_history(
        self,
        *,
        body_markers: Sequence[str],
        pr_numbers: Sequence[int],
        revision_limit: int | None,
    ) -> tuple[
        dict[str, dict[int, GithubIssueComment | None]],
        dict[int, tuple[GithubPRRevision, ...]],
    ]:
        numbers = sorted(set(pr_numbers))
        markers = tuple(dict.fromkeys(body_markers))
        comments_by_marker: dict[str, dict[int, GithubIssueComment | None]] = {
            marker: {number: None for number in numbers} for marker in markers
        }
        revisions_by_pr: dict[int, tuple[GithubPRRevision, ...]] = {
            number: () for number in numbers
        }
        for chunk in batched(numbers, _GRAPHQL_PR_BATCH_SIZE, strict=False):
            pending_comments: dict[int, str | None] = (
                {number: None for number in chunk} if markers else {}
            )
            pending_revisions = (
                dict.fromkeys(chunk, revision_limit) if revision_limit is not None else {}
            )
            while pending_comments or pending_revisions:
                request_numbers = sorted(pending_comments.keys() | pending_revisions.keys())
                query, cursor_variables = _pr_history_query(
                    comments_cursors=pending_comments,
                    revision_limits=pending_revisions,
                )
                payload = await self._graphql_query(
                    query,
                    response_name="pull request history lookup",
                    tolerate_missing_selections=True,
                    variables={**self._repo_variables, **cursor_variables},
                )
                repo = _graphql_repo_payload(
                    payload,
                    response_name="pull request history lookup",
                )
                for number in request_numbers:
                    alias = f"pr_{number}"
                    history = _pr_history_from_graphql(
                        alias=alias,
                        raw_pr=repo.get(alias),
                        response_name="pull request history lookup",
                    )
                    if number in pending_comments:
                        comments, cursor = _issue_comments_from_graphql(history, alias=alias)
                        for marker in markers:
                            if comments_by_marker[marker][number] is None:
                                comments_by_marker[marker][number] = next(
                                    (comment for comment in comments if marker in comment.body),
                                    None,
                                )
                        if cursor is None or all(
                            comments_by_marker[marker][number] is not None for marker in markers
                        ):
                            pending_comments.pop(number)
                        else:
                            pending_comments[number] = cursor
                    if number in pending_revisions:
                        revisions_by_pr[number] = _revisions_from_graphql(history)
                        del pending_revisions[number]
        return comments_by_marker, revisions_by_pr

    async def create_issue_comment(
        self,
        *,
        issue_number: int,
        body: str,
    ) -> None:
        response = await self._request(
            "POST",
            f"{self._repo_path}/issues/{issue_number}/comments",
            json={"body": body},
        )
        _expect_success(response)

    async def update_issue_comment(
        self,
        *,
        comment_id: int,
        body: str,
    ) -> None:
        response = await self._request(
            "PATCH",
            f"{self._repo_path}/issues/comments/{comment_id}",
            json={"body": body},
        )
        _expect_success(response)

    async def delete_issue_comment(
        self,
        *,
        comment_id: int,
    ) -> None:
        response = await self._request(
            "DELETE",
            f"{self._repo_path}/issues/comments/{comment_id}",
        )
        _expect_success(response)

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
        _expect_success(response)

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
        _expect_success(response)

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
        return _validate_model(
            self._expect_json_payload(response, response_name="pull request update"),
            model=GithubPR,
            error_context="GitHub pull request update response had invalid data",
        )

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
        expected_head_sha: CommitId,
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
            except ValueError as error:
                raise GithubClientError(
                    "GitHub's already-pending merge response was not valid JSON.",
                    status_code=409,
                ) from error
        else:
            payload = self._expect_json_payload(
                response,
                response_name="stack merge submission",
            )
        return GithubStackMergeSubmission(
            already_pending=already_pending,
            result=_validate_model(
                payload,
                model=GithubStackMerge,
                error_context="GitHub stack merge response had invalid data",
            ),
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
        return _validate_model(
            self._expect_json_payload(response, response_name="stack merge poll"),
            model=GithubStackMerge,
            error_context="GitHub stack merge response had invalid data",
        )

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
        _expect_success(response)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> httpx2.Response:
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                )
            except httpx2.RequestError as error:
                raise GithubClientError(f"could not reach GitHub: {error}") from error

            retry_after_seconds = _retry_after_seconds(
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
            if retry_after_seconds >= _RATE_LIMIT_NOTICE_SECONDS:
                # Without this the user sees nothing but a stalled spinner and cannot tell
                # a rate-limit wait from a hang.
                logger.warning(
                    "GitHub rate limit reached. Waiting %.0fs before retry %d of %d; "
                    "interrupt and rerun later if the wait is too long.",
                    retry_after_seconds,
                    attempt + 1,
                    _DEFAULT_RATE_LIMIT_RETRIES,
                )
            await asyncio.sleep(retry_after_seconds)
            attempt += 1

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
        tolerate_missing_selections: bool = False,
        variables: dict[str, object],
    ) -> dict[str, object]:
        response = await self._request(
            "POST",
            "/graphql",
            json={
                "query": query,
                "variables": variables,
            },
        )
        payload = self._expect_json_payload(response, response_name=response_name)
        envelope = _validate_model(
            payload,
            model=_GraphqlResponse,
            error_context=f"GitHub {response_name} response had invalid data",
        )
        errors = envelope.errors
        if errors and not (tolerate_missing_selections and _only_unresolvable_aliases(errors)):
            summary = "; ".join(error.message for error in errors)
            raise GithubClientError(
                f"GitHub {response_name} failed: {summary}", graphql_errors=errors
            )
        if envelope.data is None:
            raise GithubClientError(f"GitHub {response_name} response was missing `data`.")
        return envelope.data

    def _expect_json_payload(
        self,
        response: httpx2.Response,
        *,
        response_name: str,
    ) -> object:
        """Read a successful response's JSON body, or fail closed if it has none.

        A proxy or maintenance page can answer 200 with an HTML body, so a body read goes
        through this guard rather than calling `response.json()` on its own. The two reads
        that deliberately inspect a *failed* response instead - the 422 branch-at-commit
        probe and the 409 already-pending merge - carry their own guards.
        """

        _expect_success(response)
        try:
            return response.json()
        except ValueError as error:
            raise GithubClientError(
                f"GitHub {response_name} response was not valid JSON."
            ) from error


def _expect_success(response: httpx2.Response) -> None:
    """Fail closed on a failed request, for callers that ignore the response body."""

    try:
        response.raise_for_status()
    except httpx2.HTTPStatusError as error:
        rate_limit, reset_seconds = _rate_limit_refusal(error.response)
        raise GithubClientError(
            f"GitHub request failed: {error.response.status_code}",
            body=error.response.text,
            rate_limit=rate_limit,
            rate_limit_reset_seconds=reset_seconds,
            status_code=error.response.status_code,
        ) from error


def _retry_after_seconds(*, attempt: int, response: httpx2.Response) -> float | None:
    if not _is_retryable_rate_limit(response):
        return None
    if attempt >= _DEFAULT_RATE_LIMIT_RETRIES:
        return None

    wait_seconds = _parse_retry_after_header(response.headers.get("Retry-After"))
    if wait_seconds is None:
        wait_seconds = _seconds_until_rate_limit_reset(response.headers.get("X-RateLimit-Reset"))
    if wait_seconds is None:
        wait_seconds = _DEFAULT_RATE_LIMIT_BACKOFF_SECONDS * (2**attempt)
    # GitHub's primary limit resets up to an hour out, and it asks to be waited out
    # verbatim. Honouring that would sleep for hours across the retries, so cap every
    # wait and let the retries run out instead.
    return min(wait_seconds, _MAX_RATE_LIMIT_WAIT_SECONDS)


def _is_retryable_rate_limit(response: httpx2.Response) -> bool:
    """Whether waiting could clear this response. May over-answer; see `_rate_limit_refusal`.

    `X-RateLimit-Reset` is deliberately not evidence here. GitHub sends it on nearly every
    REST response, including permissions failures that no amount of waiting fixes.
    """

    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if "Retry-After" in response.headers:
        return True
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    return "rate limit" in response.text.lower()


def _rate_limit_refusal(
    response: httpx2.Response,
) -> tuple[RateLimitKind | None, float | None]:
    """Which GitHub rate limit refused this request, and how long it says that lasts.

    A primary limit is the quota: it reports `X-RateLimit-Remaining: 0` and lifts at
    `X-RateLimit-Reset`. A secondary limit is a short burst refusal that says so in the body
    and may carry `Retry-After`; its quota headers still describe an untouched primary
    window, so that reset must not be borrowed to describe it.

    This deliberately does not reuse `_is_retryable_rate_limit`. "Should I retry?" is allowed
    to over-answer because a wasted retry is cheap, but "was this a rate limit?" is not:
    every 403 carries `X-RateLimit-Reset`, so sharing that predicate reported a permissions
    or SAML refusal as an hour-long rate limit.
    """

    if response.status_code not in {403, 429}:
        return None, None
    if response.headers.get("X-RateLimit-Remaining") == "0":
        reset = _seconds_until_rate_limit_reset(response.headers.get("X-RateLimit-Reset"))
        return "primary", reset
    if "Retry-After" in response.headers or "rate limit" in response.text.lower():
        return "secondary", _parse_retry_after_header(response.headers.get("Retry-After"))
    return None, None


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


def _only_unresolvable_aliases(errors: tuple[GraphqlError, ...]) -> bool:
    """Whether every GraphQL error only says one selection inside the repo is missing.

    GitHub answers an unresolvable `pullRequest(number:)` alias with `null` in `data` plus a
    `NOT_FOUND` error naming that alias, which the pull request lookups read as "no such pull
    request". A `NOT_FOUND` for the repository itself, and every other error, stays fatal;
    only those lookups opt in, because a caller that reads a dropped selection as an absent
    branch or an absent merge queue must not silently lose one.
    """

    return all(
        error.type == "NOT_FOUND" and len(error.path) == 2 and error.path[0] == "repository"
        for error in errors
    )


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
    return _validate_model(
        raw_pr,
        model=GithubPR,
        error_context=f"GitHub {response_name} response had invalid mutation data",
    )


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


def _branch_targets_query(branches: Sequence[str]) -> tuple[str, dict[str, str]]:
    variables: dict[str, str] = {}
    selections: list[str] = []
    for index, branch in enumerate(branches):
        name = f"qualified_{index}"
        variables[name] = f"refs/heads/{branch}"
        selections.append(
            _graphql_document(
                f"""
                branch_{index}: ref(qualifiedName: ${name}) {{
                  name
                  prefix
                  target {{
                    oid
                  }}
                }}
                """
            ).strip()
        )
    return (
        _repo_graphql_query(
            operation_name="BranchTargets",
            selections="\n\n".join(selections),
            string_variables=tuple(variables),
        ),
        variables,
    )


def _branch_targets_by_suffix_query(
    *,
    after_cursors: Sequence[str | None],
    branch_prefix: str,
    suffixes: Sequence[str],
) -> tuple[str, dict[str, str]]:
    variables: dict[str, str] = {"ref_prefix": f"refs/heads/{branch_prefix}"}
    selections: list[str] = []
    for index, (suffix, cursor) in enumerate(zip(suffixes, after_cursors, strict=True)):
        suffix_name = f"suffix_{index}"
        after = ""
        if cursor is not None:
            cursor_name = f"cursor_{index}"
            variables[cursor_name] = cursor
            after = f"after: ${cursor_name},"
        variables[suffix_name] = suffix
        selections.append(
            _graphql_document(
                f"""
                suffix_{index}: refs(
                  {after}
                  first: 100,
                  query: ${suffix_name},
                  refPrefix: $ref_prefix
                ) {{
                  nodes {{
                    name
                    prefix
                    target {{
                      oid
                    }}
                  }}
                  pageInfo {{
                    endCursor
                    hasNextPage
                  }}
                }}
                """
            ).strip()
        )
    return (
        _repo_graphql_query(
            operation_name="BranchTargetsBySuffix",
            selections="\n\n".join(selections),
            string_variables=tuple(variables),
        ),
        variables,
    )


def _prs_by_ref_query(
    aliases: dict[str, str],
    *,
    base: bool,
) -> tuple[str, dict[str, str]]:
    # `pullRequests(headRefName:)` also returns fork pull requests whose head branch has the
    # same name, oldest first, and the head-label filter discards those only after GitHub has
    # already truncated the page. Ask for a full page either way so foreign heads cannot push
    # this repo's own pull request out of view and make submit create a duplicate.
    # A base-ref lookup decides whether deleting a branch would strand a pull request that
    # names it. GitHub refuses to reopen a pull request whose base branch is gone, and refuses
    # to retarget a closed one at all, so a closed dependent is stranded exactly as permanently
    # as an open one unless its own head branch is already gone (`headRef` is null), in which
    # case it can never be reopened anyway. A merged dependent's state can never change, so its
    # base branch is free.
    operation_name = "PullRequestsByBaseRef" if base else "OpenPullRequestsByHeadRef"
    ref_argument = "baseRefName" if base else "headRefName"
    states = "[OPEN, CLOSED]" if base else "[OPEN]"
    variables: dict[str, str] = {}
    selections: list[str] = []
    for index, (alias, ref) in enumerate(aliases.items()):
        name = f"ref_{index}"
        variables[name] = ref
        selections.append(
            _graphql_document(
                f"""
                {alias}: pullRequests(
                  first: 100,
                  states: {states},
                  {ref_argument}: ${name}
                ) {{
                  nodes {{
                    ...PullRequestFields
                  }}
                }}
                """
            ).strip()
        )
    return (
        _with_pr_fields_fragment(
            _repo_graphql_query(
                operation_name=operation_name,
                selections="\n\n".join(selections),
                string_variables=tuple(variables),
            )
        ),
        variables,
    )


def _pr_history_query(
    *,
    comments_cursors: dict[int, str | None],
    revision_limits: dict[int, int],
) -> tuple[str, dict[str, str]]:
    variables: dict[str, str] = {}
    selections: list[str] = []
    numbers = sorted(comments_cursors.keys() | revision_limits.keys())
    for number in numbers:
        fields: list[str] = []
        if number in comments_cursors:
            comments_after = ""
            if (comments_cursor := comments_cursors[number]) is not None:
                name = f"comments_cursor_{number}"
                variables[name] = comments_cursor
                comments_after = f", after: ${name}"
            fields.append(
                _graphql_document(
                    f"""
                    comments(first: 100{comments_after}) {{
                      nodes {{
                        databaseId
                        body
                      }}
                      pageInfo {{
                        endCursor
                        hasNextPage
                      }}
                    }}
                    """
                ).strip()
            )
        if number in revision_limits:
            fields.append(
                _graphql_document(
                    f"""
                    timelineItems(
                      last: {revision_limits[number]},
                      itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]
                    ) {{
                      filteredCount
                      nodes {{
                        ... on HeadRefForcePushedEvent {{
                          afterCommit {{ oid }}
                          beforeCommit {{ oid }}
                        }}
                      }}
                    }}
                    """
                ).strip()
            )
        selections.append(
            "\n".join(
                (
                    f"pr_{number}: pullRequest(number: {number}) {{",
                    indent("\n".join(fields), "  "),
                    "}",
                )
            )
        )
    return (
        _repo_graphql_query(
            operation_name="PullRequestHistory",
            selections="\n\n".join(selections),
            string_variables=tuple(variables),
        ),
        variables,
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
          statusCheckRollup {
            state
          }
          url
          title
          body
          baseRefName
          headRefName
          headRefOid
          headRef {
            name
          }
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


def _repo_graphql_query(
    *,
    operation_name: str,
    selections: str,
    string_variables: Sequence[str] = (),
) -> str:
    declarations = ", ".join(
        (
            "$owner: String!",
            "$repo: String!",
            *(f"${name}: String!" for name in string_variables),
        )
    )
    return "\n".join(
        [
            f"query {operation_name}({declarations}) {{",
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
    parsed = _validate_model(
        connection,
        model=_GraphqlPRConnection,
        error_context=(
            f"GitHub {response_name} response had invalid connection payload for {alias}"
        ),
    )
    prs: list[GithubPR] = []
    for pr in parsed.nodes:
        if expected_head_label is not None and pr.head.label != expected_head_label:
            continue
        prs.append(pr)
    return tuple(prs)


def _branch_target_from_graphql(
    raw_ref: object,
    *,
    response_name: str,
) -> tuple[str, CommitId]:
    parsed = _validate_model(
        raw_ref,
        model=_GraphqlRef,
        error_context=f"GitHub {response_name} response had invalid ref data",
    )
    return _branch_target(parsed)


def _branch_target(ref: _GraphqlRef) -> tuple[str, CommitId]:
    qualified = f"{ref.prefix}{ref.name}"
    if not qualified.startswith("refs/heads/"):
        raise GithubClientError("GitHub branch lookup returned a non-branch ref.")
    return qualified.removeprefix("refs/heads/"), ref.target.oid


def build_github_client(*, repo: GithubRepoAddress) -> GithubClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jj-stack/dev",
    }
    if token := github_token():
        headers["Authorization"] = f"Bearer {token}"

    return GithubClient(
        httpx2.AsyncClient(
            base_url=GITHUB_API_BASE_URL,
            headers=headers,
            timeout=30.0,
        ),
        repo=repo,
    )


def _pr_history_from_graphql(
    *,
    alias: str,
    raw_pr: object,
    response_name: str,
) -> _GraphqlPRHistory | None:
    if raw_pr is None:
        return None
    return _validate_model(
        raw_pr,
        model=_GraphqlPRHistory,
        error_context=(
            f"GitHub {response_name} response had invalid pull request payload for {alias}"
        ),
    )


def _issue_comments_from_graphql(
    history: _GraphqlPRHistory | None,
    *,
    alias: str,
) -> tuple[tuple[GithubIssueComment, ...], str | None]:
    comments = history.comments if history is not None else None
    if comments is None:
        return (), None
    valid_comments = tuple(comment for comment in comments.nodes or () if comment is not None)
    if not comments.page_info.has_next_page:
        return valid_comments, None
    cursor = comments.page_info.end_cursor
    if cursor is None:
        raise GithubClientError(
            f"GitHub pull request history lookup response had no page cursor for {alias}."
        )
    return valid_comments, cursor


def _revisions_from_graphql(
    history: _GraphqlPRHistory | None,
) -> tuple[GithubPRRevision, ...]:
    timeline = history.timeline_items if history is not None else None
    if timeline is None:
        return ()
    nodes = timeline.nodes or ()
    return tuple(
        GithubPRRevision(
            before_commit_id=event.before_commit.oid,
            commit_id=event.after_commit.oid,
            is_current=index == len(nodes) - 1,
            version=timeline.filtered_count - len(nodes) + index + 2,
        )
        for index, event in enumerate(nodes)
        if event is not None
        and event.before_commit is not None
        and event.after_commit is not None
    )


def _validate_stack_payload(payload: object, *, response_name: str) -> GithubStack:
    number = payload.get("number") if isinstance(payload, dict) else None
    named = f"stack #{number}" if isinstance(number, int) else "one stack"
    return _validate_model(
        payload,
        model=GithubStack,
        error_context=f"GitHub {response_name} response had unusable data for {named}",
    )


def _validate_model[ResponseModel: BaseModel](
    payload: object,
    *,
    model: type[ResponseModel],
    error_context: str,
) -> ResponseModel:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        reasons = "; ".join(
            detail["msg"].removeprefix("Value error, ") for detail in error.errors()
        )
        raise GithubClientError(f"{error_context}: {reasons}.") from error
