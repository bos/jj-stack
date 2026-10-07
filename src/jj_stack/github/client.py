"""Minimal async GitHub API client."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from itertools import batched
from math import ceil
from textwrap import dedent, indent, shorten
from types import MappingProxyType
from typing import Literal

import httpx2
from pydantic import AliasPath, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, wait_for_read_tasks
from jj_stack.config import MergeMethod
from jj_stack.errors import EXIT_GITHUB, SummarizedError
from jj_stack.github.auth import github_token
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import CommitId
from jj_stack.models.github import (
    DEFAULT_PR_TEMPLATE_PATHS,
    GithubIssueComment,
    GithubPR,
    GithubPRReview,
    GithubPRRevision,
    GithubRepo,
    GithubStack,
    GithubStackMerge,
    GithubStackMergeSubmission,
)
from jj_stack.models.github_details import GithubCheck, GithubPRMergeDetails, GithubReviewThread
from jj_stack.timing import timed

logger = logging.getLogger(__name__)
GITHUB_API_BASE_URL = "https://api.github.com"

type RateLimitKind = Literal["primary", "secondary"]
# GitHub answers a query in time roughly proportional to its pull request aliases, so
# lookups split their inputs into small chunks that `_query_chunks` runs together.
_GRAPHQL_PR_BATCH_SIZE = 8
# GitHub's largest page.
PR_PAGE_SIZE = 100

REPO_NOT_FOUND_REASON = (
    "repo not found or inaccessible - check GITHUB_TOKEN, GH_TOKEN, or your gh login"
)
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


class _GraphqlTemplateBlob(BaseModel):
    text: str | None


class _GraphqlPRTemplate(BaseModel):
    repository: dict[str, _GraphqlTemplateBlob | None]


class _GraphqlPRTemplates(BaseModel):
    templates: tuple[_GraphqlPRTemplate, ...] = Field(alias="pullRequestTemplates")


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
            return "authentication failed - check GITHUB_TOKEN, GH_TOKEN, or your gh login"
        if self.status_code == 403:
            # GitHub refuses a rate-limited request with the same status as a token problem,
            # and the retries give up long before a primary limit resets.
            if self.rate_limit is not None:
                reset = self.rate_limit_reset_seconds
                minutes = None if reset is None else max(1, ceil(reset / 60))
                resets = "" if minutes is None else f", resets in about {minutes} min"
                return f"GitHub {self.rate_limit} rate limit reached{resets} - rerun later"
            return "access denied - check that your token can access the repo"
        if self.is_repo_not_found():
            return REPO_NOT_FOUND_REASON
        return f"request failed ({self.request_failure_detail()})"


class _GraphqlPRConnection(BaseModel):
    nodes: tuple[GithubPR, ...]


class _GraphqlNode(BaseModel):
    id: str


class _GraphqlPRMutationResult(BaseModel):
    pull_request: GithubPR = Field(alias="pullRequest")


class _GraphqlPageInfo(BaseModel):
    end_cursor: str | None = Field(default=None, alias="endCursor")
    has_next_page: bool = Field(default=False, alias="hasNextPage")

    @property
    def next_cursor(self) -> str | None:
        if self.has_next_page and not self.end_cursor:
            raise GithubClientError("GitHub reported another page without a pagination cursor.")
        return self.end_cursor if self.has_next_page else None


class _GraphqlConnection[NodeT](BaseModel):
    nodes: tuple[NodeT | None, ...]
    page_info: _GraphqlPageInfo = Field(alias="pageInfo")


class _GraphqlCheckDetails(BaseModel):
    contexts: _GraphqlConnection[GithubCheck]


class _GraphqlRequiredCheck(BaseModel):
    context: str


class _GraphqlRuleParameters(BaseModel):
    checks: tuple[_GraphqlRequiredCheck, ...] = Field(default=(), alias="requiredStatusChecks")
    resolve_threads: bool = Field(default=False, alias="requiredReviewThreadResolution")


class _GraphqlMergeRule(BaseModel):
    type: str
    parameters: _GraphqlRuleParameters | None = None


class _GraphqlMergeRules(BaseModel):
    nodes: tuple[_GraphqlMergeRule | None, ...]


class _GraphqlRuledRef(BaseModel):
    rules: _GraphqlMergeRules


class _GraphqlBaseBranchMergeQueue(BaseModel):
    merge_queue: _GraphqlNode | None = Field(alias="mergeQueue")
    ref: _GraphqlRuledRef | None


class _GraphqlRefUpdateRule(BaseModel):
    """Branch protection as it applies to the viewer, who may be allowed to bypass it."""

    checks: tuple[str, ...] | None = Field(default=None, alias="requiredStatusCheckContexts")
    resolve_threads: bool = Field(alias="requiresConversationResolution")


class _GraphqlBranchRules(BaseModel):
    """Branch protection combined with the active ruleset rules on one branch."""

    protection: _GraphqlRefUpdateRule | None = Field(default=None, alias="refUpdateRule")
    rules: tuple[_GraphqlMergeRule, ...] = Field(
        default=(), validation_alias=AliasPath("rules", "nodes")
    )

    @property
    def required_checks(self) -> tuple[str, ...]:
        protected = (self.protection.checks or ()) if self.protection else ()
        ruled = (
            check.context
            for rule in self.rules
            if rule.parameters
            for check in rule.parameters.checks
        )
        return tuple(dict.fromkeys((*protected, *ruled)))

    @property
    def resolve_threads(self) -> bool:
        return bool(self.protection and self.protection.resolve_threads) or any(
            rule.type == "REQUIRED_REVIEW_THREAD_RESOLUTION"
            or (rule.parameters is not None and rule.parameters.resolve_threads)
            for rule in self.rules
        )


class _GraphqlReview(BaseModel):
    state: str


class _GraphqlPRProgress(BaseModel):
    reviews: tuple[_GraphqlReview, ...] = Field(
        validation_alias=AliasPath("latestOpinionatedReviews", "nodes")
    )
    behind: int | None = Field(
        default=None, validation_alias=AliasPath("headRef", "compare", "aheadBy")
    )

    @property
    def approvals(self) -> int:
        return sum(review.state == "APPROVED" for review in self.reviews)


class _GraphqlTestMerge(BaseModel):
    oid: CommitId
    checks: _GraphqlCheckDetails | None = Field(default=None, alias="statusCheckRollup")


class _GraphqlPRMergeDetails(BaseModel):
    head: CommitId = Field(alias="headRefOid")
    base_name: str = Field(alias="baseRefName")
    mergeable: str | None = None
    test_merge: _GraphqlTestMerge | None = Field(default=None, alias="potentialMergeCommit")
    threads: _GraphqlConnection[GithubReviewThread] | None = Field(
        default=None, alias="reviewThreads"
    )
    checks: _GraphqlCheckDetails | None = Field(default=None, alias="statusCheckRollup")


class _GraphqlGitObject(BaseModel):
    oid: CommitId


class _GraphqlRefTarget(BaseModel):
    target: _GraphqlGitObject


class _GraphqlRef(_GraphqlRefTarget):
    name: str
    prefix: str


class _GraphqlForcePushEvent(BaseModel):
    after_commit: _GraphqlGitObject | None = Field(default=None, alias="afterCommit")
    before_commit: _GraphqlGitObject | None = Field(default=None, alias="beforeCommit")


class _GraphqlTimelineItemConnection(BaseModel):
    filtered_count: int = Field(alias="filteredCount")
    nodes: tuple[_GraphqlForcePushEvent | None, ...] | None = None


class _GraphqlPRHistory(BaseModel):
    comments: _GraphqlConnection[GithubIssueComment] | None = None
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
        await self._client.aclose()

    async def get_repo(self) -> GithubRepo:
        response = await self._request("GET", self._repo_path)
        return _response_model(response, model=GithubRepo, response_name="repo lookup")

    async def get_pr_template(self) -> str:
        """Read GitHub's default template, including the owner's public .github fallback."""

        _, template = await self.get_publication_branches(branches=(), include_pr_template=True)
        return template

    async def get_branch_targets(
        self,
        *,
        branches: Sequence[str],
    ) -> dict[str, CommitId]:
        """Return exact GitHub branch targets without advertising unrelated refs."""

        targets, _ = await self.get_publication_branches(
            branches=branches, include_pr_template=False
        )
        return targets

    async def get_publication_branches(
        self,
        *,
        branches: Sequence[str],
        include_pr_template: bool,
    ) -> tuple[dict[str, CommitId], str]:
        """Read branch targets and optionally the default PR template in the same request."""

        ordered = tuple(dict.fromkeys(branches))
        targets: dict[str, CommitId] = {}
        template = ""

        async def query_chunk(chunk: tuple[str, ...]) -> None:
            nonlocal template
            read_template = include_pr_template and (not ordered or chunk[0] == ordered[0])
            query, branch_variables = _branch_targets_query(
                chunk, include_pr_template=read_template
            )
            repo = await self._graphql_repo(
                query,
                model=dict[str, object],
                response_name="branch target lookup",
                variables=branch_variables,
            )
            if read_template:
                template = _default_pr_template(repo)
            for index, branch in enumerate(chunk):
                if (raw_ref := repo.get(f"branch_{index}")) is not None:
                    targets[branch] = _validate_model(
                        raw_ref,
                        model=_GraphqlRefTarget,
                        error_context="GitHub branch target lookup response had invalid ref data",
                    ).target.oid

        if ordered:
            await _query_chunks(ordered, query_chunk)
        elif include_pr_template:
            await query_chunk(())
        return targets, template

    async def get_publication_branches_by_suffix(
        self,
        *,
        branch_prefix: str,
        suffixes: Sequence[str],
        include_pr_template: bool,
    ) -> tuple[dict[str, CommitId], str]:
        """Read recovery branch targets and optionally the default PR template together."""

        ordered = tuple(dict.fromkeys(suffixes))
        targets: dict[str, CommitId] = {}
        template = ""

        async def query_chunk(chunk: tuple[str, ...]) -> None:
            nonlocal template
            pending: tuple[tuple[str, str | None], ...] = tuple(
                (suffix, None) for suffix in chunk
            )
            while pending:
                read_template = include_pr_template and pending[0] == (ordered[0], None)
                query, suffix_variables = _branch_targets_by_suffix_query(
                    after_cursors=tuple(cursor for _suffix, cursor in pending),
                    branch_prefix=branch_prefix,
                    suffixes=tuple(suffix for suffix, _cursor in pending),
                    include_pr_template=read_template,
                )
                repo = await self._graphql_repo(
                    query,
                    model=dict[str, object],
                    response_name="branch suffix lookup",
                    variables=suffix_variables,
                )
                if read_template:
                    template = _default_pr_template(repo)
                next_page: list[tuple[str, str]] = []
                for index, (suffix, _cursor) in enumerate(pending):
                    connection = _validate_model(
                        repo.get(f"suffix_{index}"),
                        model=_GraphqlConnection[_GraphqlRef],
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
                    if (cursor := connection.page_info.next_cursor) is not None:
                        next_page.append((suffix, cursor))
                pending = tuple(next_page)

        await _query_chunks(ordered, query_chunk)
        return targets, template

    async def list_stacks(self) -> tuple[GithubStack, ...]:
        return await self._get_paginated(
            f"{self._repo_path}/stacks", model=tuple[GithubStack, ...], response_name="stack list"
        )

    async def get_stack(self, *, stack_number: int) -> GithubStack:
        response = await self._request("GET", f"{self._repo_path}/stacks/{stack_number}")
        return _response_model(response, model=GithubStack, response_name="stack lookup")

    async def create_stack(self, *, pr_numbers: Sequence[int]) -> GithubStack:
        response = await self._request(
            "POST",
            f"{self._repo_path}/stacks",
            json={"pull_requests": list(pr_numbers)},
        )
        return _response_model(response, model=GithubStack, response_name="stack creation")

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
        return _response_model(response, model=GithubStack, response_name="stack append")

    async def unstack(self, *, stack_number: int) -> GithubStack | None:
        response = await self._request(
            "POST",
            f"{self._repo_path}/stacks/{stack_number}/unstack",
        )
        if response.status_code == 204:
            return None
        return _response_model(response, model=GithubStack, response_name="unstack")

    async def get_pr(
        self,
        *,
        pr_number: int,
    ) -> GithubPR:
        pr = (await self.get_prs_by_numbers(pr_numbers=(pr_number,)))[pr_number]
        if pr is None:
            raise GithubClientError(f"GitHub has no pull request #{pr_number}.")
        return pr

    async def get_prs_by_numbers(
        self,
        *,
        pr_numbers: Sequence[int],
        merge_progress: bool = False,
    ) -> dict[int, GithubPR | None]:
        numbers = sorted(set(pr_numbers))
        results: dict[int, GithubPR | None] = {}

        async def query_chunk(chunk: tuple[int, ...]) -> None:
            query = _prs_by_number_query(chunk, merge_progress=merge_progress)
            repo = await self._graphql_repo(
                query,
                model=dict[str, GithubPR | None],
                response_name="pull request batch lookup",
                tolerate_missing_selections=True,
            )
            for number in chunk:
                results[number] = repo.get(f"pr_{number}")

        await _query_chunks(numbers, query_chunk)
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
        kind = "base" if base else "head"
        response_name = f"pull request {kind} lookup"
        results: dict[str, tuple[GithubPR, ...]] = {}

        async def query_chunk(chunk: tuple[str, ...]) -> None:
            aliases = {f"{kind}_{index}": ref for index, ref in enumerate(chunk)}
            query, ref_variables = _prs_by_ref_query(aliases, base=base)
            repo = await self._graphql_repo(
                query,
                model=dict[str, _GraphqlPRConnection],
                response_name=response_name,
                variables=ref_variables,
            )
            for alias, ref in aliases.items():
                head_label = f"{self._repo.owner}:{ref}"
                results[ref] = tuple(
                    pr for pr in repo[alias].nodes if base or pr.head.label == head_label
                )

        await _query_chunks(refs, query_chunk)
        return results

    async def create_pr(
        self,
        *,
        base: str,
        body: str,
        draft: bool,
        head: str,
        repository_id: str,
        title: str,
    ) -> GithubPR:
        return await self._pr_mutation(
            "createPullRequest",
            fields={
                "baseRefName": base,
                "body": body,
                "draft": draft,
                "headRefName": head,
                "repositoryId": repository_id,
                "title": title,
            },
            response_name="pull request creation",
        )

    async def list_pr_reviews(
        self,
        *,
        pr_number: int,
    ) -> tuple[GithubPRReview, ...]:
        return await self._get_paginated(
            f"{self._repo_path}/pulls/{pr_number}/reviews",
            model=tuple[GithubPRReview, ...],
            response_name="pull request reviews",
        )

    async def find_issue_comments_by_body_marker(
        self,
        *,
        body_marker: str,
        pr_numbers: Sequence[int],
    ) -> dict[int, GithubIssueComment | None]:
        comments_by_marker, _revisions = await self.find_issue_comments_and_revisions(
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
        revision_limit: int | None,
    ) -> tuple[
        dict[str, dict[int, GithubIssueComment | None]],
        dict[int, tuple[GithubPRRevision, ...]],
    ]:
        """Batch managed-comment lookups with recent PR revisions."""

        numbers = sorted(set(pr_numbers))
        markers = tuple(dict.fromkeys(body_markers))
        comments_by_marker: dict[str, dict[int, GithubIssueComment | None]] = {
            marker: {number: None for number in numbers} for marker in markers
        }
        revisions_by_pr: dict[int, tuple[GithubPRRevision, ...]] = {
            number: () for number in numbers
        }

        async def query_chunk(chunk: tuple[int, ...]) -> None:
            pending_comments: dict[int, str | None] = dict.fromkeys(chunk)
            pending_revisions = (
                dict.fromkeys(chunk, revision_limit) if revision_limit is not None else {}
            )
            while pending_comments or pending_revisions:
                request_numbers = sorted(pending_comments.keys() | pending_revisions.keys())
                query, cursor_variables = _pr_history_query(
                    comments_cursors=pending_comments,
                    revision_limits=pending_revisions,
                )
                repo = await self._graphql_repo(
                    query,
                    model=dict[str, _GraphqlPRHistory | None],
                    response_name="pull request history lookup",
                    tolerate_missing_selections=True,
                    variables=cursor_variables,
                )
                for number in request_numbers:
                    history = repo.get(f"pr_{number}")
                    if number in pending_comments:
                        comments, cursor = _issue_comments_from_graphql(history)
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

        await _query_chunks(numbers, query_chunk)
        return comments_by_marker, revisions_by_pr

    async def get_pr_merge_details(
        self, *, prs: Sequence[GithubPR]
    ) -> dict[int, GithubPRMergeDetails | None]:
        """Batch merge evidence for the observed PR heads and bases.

        GitHub applies the rules of the branch a stack lands on to every PR in it, so each PR's
        requirements come from that branch rather than its own base.
        """

        results: dict[int, GithubPRMergeDetails | None] = {}

        async def query_chunk(chunk: tuple[GithubPR, ...]) -> None:
            heads = {pr.number: pr.head.sha for pr in chunk}
            bases = {pr.number: pr.base.ref for pr in chunk}
            merge_commits: dict[int, CommitId | None] = {}
            pending_threads: dict[int, str | None] = dict.fromkeys(heads)
            pending_checks: dict[int, str | None] = dict.fromkeys(heads)
            pending_merge_checks: dict[int, str | None] = dict.fromkeys(heads)
            cursors = (pending_threads, pending_checks, pending_merge_checks)
            while any(cursors):
                numbers = sorted(set().union(*cursors))
                query, variables = _pr_merge_details_query(
                    pending_threads, pending_checks, pending_merge_checks
                )
                repo = await self._graphql_repo(
                    query,
                    model=dict[str, _GraphqlPRMergeDetails | None],
                    response_name="merge details lookup",
                    tolerate_missing_selections=True,
                    variables=variables,
                )
                for number in numbers:
                    page = repo.get(f"pr_{number}")
                    merge_commit = page.test_merge.oid if page and page.test_merge else None
                    if (
                        page is None
                        or page.head != heads[number]
                        or page.base_name != bases[number]
                        or merge_commits.get(number, merge_commit) != merge_commit
                    ):
                        results[number] = None
                        for pending in cursors:
                            pending.pop(number, None)
                        continue
                    merge_commits[number] = merge_commit
                    prior = results.get(number) or GithubPRMergeDetails()
                    threads = _consume_merge_details_page(number, page.threads, pending_threads)
                    checks = _consume_merge_details_page(
                        number,
                        page.checks.contexts if page.checks else None,
                        pending_checks,
                        absent_ok=True,
                    )
                    merge_rollup = page.test_merge.checks if page.test_merge else None
                    merge_checks = _consume_merge_details_page(
                        number,
                        merge_rollup.contexts if merge_rollup else None,
                        pending_merge_checks,
                        absent_ok=True,
                    )
                    results[number] = GithubPRMergeDetails(
                        mergeable=page.mergeable,
                        unresolved_threads=prior.unresolved_threads
                        + tuple(thread for thread in threads if not thread.is_resolved),
                        checks=prior.checks + checks,
                        merge_checks=prior.merge_checks + merge_checks,
                    )

        landing = {pr.number: pr.stack_base_ref or pr.base.ref for pr in prs}
        chunks = asyncio.create_task(_query_chunks(prs, query_chunk))
        rules = asyncio.create_task(self._branch_rules(sorted(set(landing.values()))))
        await wait_for_read_tasks(chunks, rules)
        branch_rules = rules.result()
        return {
            number: details
            and details.model_copy(
                update={
                    "required_checks": branch_rules[landing[number]].required_checks,
                    "resolve_threads": branch_rules[landing[number]].resolve_threads,
                }
            )
            for number, details in results.items()
        }

    async def get_pr_progress(
        self, *, prs: Sequence[GithubPR]
    ) -> dict[int, tuple[int, int | None]]:
        """Each PR's approvals from writers and, at the bottom of a stack, how far behind its
        landing branch it is."""

        results: dict[int, tuple[int, int | None]] = {}

        async def query_chunk(chunk: tuple[GithubPR, ...]) -> None:
            variables: dict[str, str] = {}
            selections: list[str] = []
            for pr in chunk:
                landing = pr.stack_base_ref or pr.base.ref
                compare = ""
                if pr.base.ref == landing:
                    variables[f"landing_{pr.number}"] = landing
                    compare = (
                        f"headRef {{ compare(headRef: $landing_{pr.number}) {{ aheadBy }} }}"
                    )
                selections.append(
                    f"""pr_{pr.number}: pullRequest(number: {pr.number}) {{
                      latestOpinionatedReviews(first: {PR_PAGE_SIZE}, writersOnly: true) {{
                        nodes {{ state }}
                      }}
                      {compare}
                    }}"""
                )
            repo = await self._graphql_repo(
                _repo_graphql_query(
                    operation_name="PullRequestProgress",
                    selections="\n".join(selections),
                    string_variables=tuple(variables),
                ),
                model=dict[str, _GraphqlPRProgress | None],
                response_name="pull request progress lookup",
                tolerate_missing_selections=True,
                variables=variables,
            )
            for pr in chunk:
                if (progress := repo.get(f"pr_{pr.number}")) is not None:
                    results[pr.number] = (progress.approvals, progress.behind)

        await _query_chunks(prs, query_chunk)
        return results

    async def _branch_rules(self, branches: Sequence[str]) -> dict[str, _GraphqlBranchRules]:
        if not branches:
            return {}
        variables = {f"ref_{index}": f"refs/heads/{name}" for index, name in enumerate(branches)}
        repo = await self._graphql_repo(
            _branch_rules_query(len(branches)),
            model=dict[str, _GraphqlBranchRules | None],
            response_name="branch rules lookup",
            variables=variables,
        )
        return {
            name: repo.get(f"branch_{index}") or _GraphqlBranchRules()
            for index, name in enumerate(branches)
        }

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
        pr_id: str,
        base: str | None,
        body: str | None = None,
        title: str | None = None,
    ) -> GithubPR:
        fields = {"baseRefName": base, "body": body, "title": title}
        return await self._pr_mutation(
            "updatePullRequest",
            fields={
                "pullRequestId": pr_id,
                **{name: value for name, value in fields.items() if value is not None},
            },
            response_name="pull request update",
        )

    async def set_pr_draft(self, *, pr_id: str, draft: bool) -> GithubPR:
        return await self._pr_mutation(
            "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview",
            fields={"pullRequestId": pr_id},
            response_name=(
                "convert pull request to draft" if draft else "mark pull request ready for review"
            ),
        )

    async def _pr_mutation(
        self, mutation: str, *, fields: dict[str, object], response_name: str
    ) -> GithubPR:
        payload = await self._graphql_query(
            _pr_mutation_document(mutation),
            response_name=response_name,
            variables={"input": fields},
        )
        return _validate_model(
            payload.get(mutation),
            model=_GraphqlPRMutationResult,
            error_context=f"GitHub {response_name} response had invalid mutation data",
        ).pull_request

    async def base_branch_uses_merge_queue(self, *, branch: str) -> bool:
        observed = await self._graphql_repo(
            _base_branch_merge_queue_query(),
            model=_GraphqlBaseBranchMergeQueue,
            response_name="base branch merge queue lookup",
            variables={"branch": branch, "qualified": f"refs/heads/{branch}"},
        )
        if observed.merge_queue is not None:
            return True
        rules = () if observed.ref is None else observed.ref.rules.nodes
        return any(rule is not None and rule.type == "MERGE_QUEUE" for rule in rules)

    async def submit_stack_merge(
        self,
        *,
        expected_head_sha: CommitId,
        method: MergeMethod | None,
        pr_number: int,
    ) -> GithubStackMergeSubmission:
        """Merge directly with method, or through the merge queue when it is None."""

        body: dict[str, object] = {"merge_action": "merge_queue", "sha": expected_head_sha}
        if method is not None:
            body |= {"merge_action": "direct_merge", "merge_method": method}
        response = await self._request(
            "PUT",
            f"{self._repo_path}/pulls/{pr_number}/merge-async",
            json=body,
        )
        # 409 means GitHub already has an operation in flight for this pull request, not that
        # the merge conflicts.
        already_pending = response.status_code == 409
        if not already_pending:
            _expect_success(response)
        return GithubStackMergeSubmission(
            already_pending=already_pending,
            result=_json_model(
                response.content,
                model=GithubStackMerge,
                response_name="stack merge",
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
        return _response_model(
            response,
            model=GithubStackMerge,
            response_name="stack merge",
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
                with timed("github", _request_label(method, path, json, attempt=attempt)):
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

    async def _get_paginated[ItemT](
        self,
        path: str,
        *,
        model: type[tuple[ItemT, ...]],
        response_name: str,
    ) -> tuple[ItemT, ...]:
        items: list[ItemT] = []
        next_path: str | None = f"{path}?per_page={PR_PAGE_SIZE}"

        while next_path is not None:
            response = await self._request("GET", next_path)
            items.extend(
                _response_model(
                    response,
                    model=model,
                    response_name=response_name,
                )
            )
            next_path = response.links.get("next", {}).get("url")

        return tuple(items)

    async def _graphql_repo[RepoT](
        self,
        query: str,
        *,
        model: type[RepoT],
        response_name: str,
        tolerate_missing_selections: bool = False,
        variables: Mapping[str, object] = MappingProxyType({}),
    ) -> RepoT:
        data = await self._graphql_query(
            query,
            response_name=response_name,
            tolerate_missing_selections=tolerate_missing_selections,
            variables={**self._repo_variables, **variables},
        )
        return _validate_model(
            data.get("repository"),
            model=model,
            error_context=f"GitHub {response_name} response had invalid repo data",
        )

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
        envelope = _response_model(
            response,
            model=_GraphqlResponse,
            response_name=response_name,
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


async def _query_chunks[ItemT](
    items: Sequence[ItemT],
    query_chunk: Callable[[tuple[ItemT, ...]], Awaitable[None]],
) -> None:
    """Run `query_chunk` over every chunk of `items` concurrently, stopping at the first failure.

    Several small queries in flight together finish sooner than one large query.
    """

    semaphore = asyncio.Semaphore(DEFAULT_BOUNDED_CONCURRENCY)

    async def bounded(chunk: tuple[ItemT, ...]) -> None:
        async with semaphore:
            await query_chunk(chunk)

    tasks = tuple(
        asyncio.create_task(bounded(chunk))
        for chunk in batched(items, _GRAPHQL_PR_BATCH_SIZE, strict=False)
    )
    await wait_for_read_tasks(*tasks)


_GRAPHQL_OPERATION = re.compile(r"\b(?:query|mutation)\s+(\w+)")


def _request_label(
    method: str, path: str, json: dict[str, object] | None, *, attempt: int
) -> str:
    query = json.get("query") if json is not None else None
    operation = _GRAPHQL_OPERATION.search(query) if isinstance(query, str) else None
    label = f"{method} {path}" if operation is None else f"{method} {path} {operation.group(1)}"
    return label if attempt == 0 else f"{label} retry {attempt}"


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


def _default_pr_template(repo: dict[str, object]) -> str:
    templates = _validate_model(
        repo,
        model=_GraphqlPRTemplates,
        error_context="GitHub pull request template response had invalid data",
    )
    for item in templates.templates:
        for index in range(len(DEFAULT_PR_TEMPLATE_PATHS)):
            blob = item.repository.get(f"default_{index}")
            if blob is not None:
                return (blob.text or "").strip()
    return ""


def _pr_template_selection() -> str:
    # GitHub chooses the effective source, including the owner's public .github repo. Its
    # template filenames omit directories, so inspect actual default paths in that source
    # rather than mistaking a file inside PULL_REQUEST_TEMPLATE/ for the default.
    files = "\n".join(
        f'default_{index}: object(expression: "HEAD:{path}") {{ ... on Blob {{ text }} }}'
        for index, path in enumerate(DEFAULT_PR_TEMPLATE_PATHS)
    )
    return f"pullRequestTemplates {{ repository {{ {files} }} }}"


def _prs_by_number_query(numbers: Sequence[int], *, merge_progress: bool) -> str:
    selections = "\n\n".join(
        _graphql_document(
            f"""
            pr_{number}: pullRequest(number: {number}) {{
              ...PullRequestFields
              {_merge_progress_fields() if merge_progress else ""}
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


def _merge_progress_fields() -> str:
    return """
      mergeQueueEntry {
        position state estimatedTimeToMerge
        mergeQueue { entries { totalCount } }
        headCommit {
          statusCheckRollup {
            contexts {
              totalCount
              checkRunCountsByState { state count }
              statusContextCountsByState { state count }
            }
          }
        }
      }
      timelineItems(
        last: 1, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]
      ) {
        nodes { ... on RemovedFromMergeQueueEvent { reason beforeCommit { oid } } }
      }
    """


def _branch_targets_query(
    branches: Sequence[str], *, include_pr_template: bool
) -> tuple[str, dict[str, str]]:
    variables: dict[str, str] = {}
    selections: list[str] = []
    if include_pr_template:
        selections.append(_pr_template_selection())
    for index, branch in enumerate(branches):
        name = f"qualified_{index}"
        variables[name] = f"refs/heads/{branch}"
        selections.append(
            _graphql_document(
                f"""
                branch_{index}: ref(qualifiedName: ${name}) {{
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
    include_pr_template: bool,
) -> tuple[str, dict[str, str]]:
    variables: dict[str, str] = {"ref_prefix": f"refs/heads/{branch_prefix}"}
    selections: list[str] = []
    if include_pr_template:
        selections.append(_pr_template_selection())
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
                  first: {PR_PAGE_SIZE},
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


def _consume_merge_details_page[NodeT](
    number: int,
    page: _GraphqlConnection[NodeT] | None,
    pending: dict[int, str | None],
    *,
    absent_ok: bool = False,
) -> tuple[NodeT, ...]:
    if number not in pending:
        return ()
    if page is None and not absent_ok:
        raise GithubClientError(f"GitHub omitted review threads for PR #{number}.")
    cursor = page.page_info.next_cursor if page is not None else None
    if cursor is None:
        del pending[number]
    else:
        pending[number] = cursor
    return tuple(node for node in page.nodes if node is not None) if page else ()


def _pr_merge_details_query(
    threads_cursors: dict[int, str | None],
    checks_cursors: dict[int, str | None],
    merge_checks_cursors: dict[int, str | None],
) -> tuple[str, dict[str, str]]:
    variables: dict[str, str] = {}
    selections: list[str] = []
    connections = (
        ("threads", threads_cursors),
        ("checks", checks_cursors),
        ("merge_checks", merge_checks_cursors),
    )
    for number in sorted(set().union(*(cursors for _, cursors in connections))):
        fields = ["headRefOid baseRefName mergeable potentialMergeCommit { oid }"]
        for kind, cursors in connections:
            if number not in cursors:
                continue
            after = ""
            if (cursor := cursors[number]) is not None:
                name = f"{kind}_{number}"
                variables[name] = cursor
                after = f", after: ${name}"
            page_info = "pageInfo { endCursor hasNextPage }"
            if kind == "threads":
                fields.append(
                    f"""reviewThreads(first: {PR_PAGE_SIZE}{after}) {{
                      nodes {{
                        isResolved isOutdated path line
                        comments(first: 1) {{ nodes {{ bodyText url }} }}
                      }}
                      {page_info}
                    }}"""
                )
            else:
                rollup = f"""statusCheckRollup {{
                      contexts(first: {PR_PAGE_SIZE}{after}) {{
                        nodes {{
                          ... on CheckRun {{ name status conclusion url: detailsUrl }}
                          ... on StatusContext {{ name: context state url: targetUrl }}
                        }}
                        {page_info}
                      }}
                    }}"""
                fields.append(
                    f"potentialMergeCommit {{ {rollup} }}" if kind == "merge_checks" else rollup
                )
        selections.append(f"pr_{number}: pullRequest(number: {number}) {{ {' '.join(fields)} }}")
    return (
        _repo_graphql_query(
            operation_name="PullRequestMergeDetails",
            selections="\n".join(selections),
            string_variables=tuple(variables),
        ),
        variables,
    )


def _branch_rules_query(count: int) -> str:
    selections = "\n".join(
        f"""branch_{index}: ref(qualifiedName: $ref_{index}) {{
          refUpdateRule {{ requiredStatusCheckContexts requiresConversationResolution }}
          rules(first: {PR_PAGE_SIZE}) {{
            nodes {{ type parameters {{
              ... on RequiredStatusChecksParameters {{ requiredStatusChecks {{ context }} }}
              ... on PullRequestParameters {{ requiredReviewThreadResolution }}
            }} }}
          }}
        }}"""
        for index in range(count)
    )
    return _repo_graphql_query(
        operation_name="BranchMergeRules",
        selections=selections,
        string_variables=tuple(f"ref_{index}" for index in range(count)),
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


def _pr_mutation_document(mutation: str) -> str:
    operation = mutation[0].upper() + mutation[1:]
    return _with_pr_fields_fragment(
        _graphql_document(
            f"""
            mutation {operation}($input: {operation}Input!) {{
              {mutation}(input: $input) {{
                pullRequest {{
                  ...PullRequestFields
                }}
              }}
            }}
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
          reviewDecision
          mergeStateStatus
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
          stack {
            baseRefName
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


def _branch_target(ref: _GraphqlRef) -> tuple[str, CommitId]:
    qualified = f"{ref.prefix}{ref.name}"
    if not qualified.startswith("refs/heads/"):
        raise GithubClientError("GitHub branch lookup returned a non-branch ref.")
    return qualified.removeprefix("refs/heads/"), ref.target.oid


def build_github_client(*, repo: GithubRepoAddress, token: str | None) -> GithubClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jj-stack/dev",
    }
    if token is None:
        token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return GithubClient(
        httpx2.AsyncClient(
            base_url=GITHUB_API_BASE_URL,
            headers=headers,
            timeout=30.0,
        ),
        repo=repo,
    )


def _issue_comments_from_graphql(
    history: _GraphqlPRHistory | None,
) -> tuple[tuple[GithubIssueComment, ...], str | None]:
    comments = history.comments if history is not None else None
    if comments is None:
        return (), None
    valid_comments = tuple(comment for comment in comments.nodes if comment is not None)
    return valid_comments, comments.page_info.next_cursor


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


def _validate_model[ResponseT](
    payload: object,
    *,
    model: type[ResponseT],
    error_context: str,
) -> ResponseT:
    try:
        return TypeAdapter(model).validate_python(payload)
    except ValidationError as error:
        raise _invalid_response(error_context, error) from error


def _response_model[ResponseT](
    response: httpx2.Response, *, model: type[ResponseT], response_name: str
) -> ResponseT:
    """Validate a successful response's JSON body.

    A proxy or maintenance page can answer 200 with an HTML body, which fails here too.
    """

    _expect_success(response)
    return _json_model(response.content, model=model, response_name=response_name)


def _json_model[ResponseT](
    content: bytes, *, model: type[ResponseT], response_name: str
) -> ResponseT:
    try:
        return TypeAdapter(model).validate_json(content)
    except ValidationError as error:
        context = f"GitHub {response_name} response had invalid data"
        raise _invalid_response(context, error) from error


def _invalid_response(error_context: str, error: ValidationError) -> GithubClientError:
    # The location names the alias, such as the pull request, that GitHub answered badly.
    reasons = "; ".join(
        " ".join((*map(str, detail["loc"][:1]), detail["msg"].removeprefix("Value error, ")))
        for detail in error.errors()
    )
    return GithubClientError(f"{error_context}: {reasons}.")
