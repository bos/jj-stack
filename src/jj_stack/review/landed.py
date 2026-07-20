"""Observational convergence for landed tracked reviews.

A tracked change is landed when its pull request is merged and its submitted commit is an
ancestor of remote trunk. For transports that preserve commit IDs (a direct trunk push, or a
merge-commit merge), the exact submitted commit reaches trunk while the pull request may still
be open; finalizing that pull request (retarget to trunk, close, confirm it is no longer open)
is idempotent, so it is equally reachable from the `land` run that pushed trunk and from any
later `land` or `sync`.

This module sweeps saved tracking for such reviews, finalizes their pull requests, retires
their records, and forgets local review bookmarks that still point at the landed commits. It
consults no saved operation state: an interrupted land converges here purely from what GitHub
and the jj DAG currently report. Anything it cannot prove safe is skipped with a reason and
left for the next run, never raised as a failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import GithubRepoAddress, resolve_trunk_branch
from jj_stack.github.stack_comments import (
    StackCommentKind,
    delete_stack_comment,
    is_navigation_comment,
    is_overview_comment,
)
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewState
from jj_stack.review.bookmarks import (
    bookmark_cleanup_allowed,
    classify_local_bookmark_forget,
)
from jj_stack.state.store import ReviewStateStore
from jj_stack.ui import Message

LandedReviewOutcome = Literal["finalized", "already_merged", "skipped"]


@dataclass(frozen=True, slots=True)
class LandedReviewCandidate:
    """One tracked review whose submitted commit is an ancestor of trunk."""

    bookmark: str
    bookmark_managed: bool
    change_id: str
    commit_id: str
    pull_request_number: int


@dataclass(frozen=True, slots=True)
class LandedReviewResult:
    """The sweep outcome for one landed tracked review."""

    candidate: LandedReviewCandidate
    outcome: LandedReviewOutcome
    forgot_bookmark: bool = False
    skip_reason: Message | None = None

    @property
    def retired(self) -> bool:
        return self.outcome != "skipped"


@dataclass(frozen=True, slots=True)
class BookmarkCleanupPolicy:
    """Which landed local review bookmarks the sweep may forget."""

    cleanup_bookmarks: bool
    cleanup_user_bookmarks: bool
    prefix: str


def landed_review_candidates(
    *,
    jj_client: JjClient,
    state: ReviewState,
    trunk_commit_id: str,
) -> tuple[LandedReviewCandidate, ...]:
    """Return tracked reviews whose exact submitted commit is an ancestor of trunk."""

    saved: list[LandedReviewCandidate] = []
    for change_id, cached in sorted(state.changes.items()):
        if cached.link_state != "active":
            continue
        if (
            cached.bookmark is None
            or cached.pr_number is None
            or cached.last_submitted_commit_id is None
        ):
            continue
        saved.append(
            LandedReviewCandidate(
                bookmark=cached.bookmark,
                bookmark_managed=cached.manages_bookmark,
                change_id=change_id,
                commit_id=cached.last_submitted_commit_id,
                pull_request_number=cached.pr_number,
            )
        )
    if not saved:
        return ()
    landed_commit_ids = jj_client.query_commit_ids_ancestors_of(
        tuple(candidate.commit_id for candidate in saved),
        descendant_commit_id=trunk_commit_id,
    )
    return tuple(candidate for candidate in saved if candidate.commit_id in landed_commit_ids)


async def finalize_landed_reviews(
    *,
    bookmark_policy: BookmarkCleanupPolicy,
    dry_run: bool = False,
    github_client: GithubClient,
    jj_client: JjClient,
    labels: dict[str, str] | None = None,
    order: tuple[str, ...] = (),
    state_store: ReviewStateStore,
    trunk_branch: str,
    trunk_commit_id: str,
) -> tuple[LandedReviewResult, ...]:
    """Finalize and retire every tracked review already landed on trunk.

    `order` lists change IDs whose finalization should happen first, bottom-up
    (a fresh land passes its planned prefix so GitHub-side state changes follow
    stack order); remaining landed reviews follow in change-ID order. `labels`
    optionally maps change IDs to subjects for progress output.

    With `dry_run`, the sweep reports what it would finalize and retire without
    mutating GitHub, bookmarks, or tracking.
    """

    state = state_store.load()
    candidates = landed_review_candidates(
        jj_client=jj_client,
        state=state,
        trunk_commit_id=trunk_commit_id,
    )
    if not candidates:
        return ()
    order_index = {change_id: index for index, change_id in enumerate(order)}
    candidates = tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                order_index.get(candidate.change_id, len(order_index)),
                candidate.change_id,
            ),
        )
    )
    bookmark_states = jj_client.list_bookmark_states(
        tuple(candidate.bookmark for candidate in candidates)
    )
    results: list[LandedReviewResult] = []
    retired_change_ids: list[str] = []
    for candidate in candidates:
        result = await _finalize_landed_review(
            bookmark_policy=bookmark_policy,
            bookmark_state=bookmark_states.get(
                candidate.bookmark, BookmarkState(name=candidate.bookmark)
            ),
            candidate=candidate,
            dry_run=dry_run,
            github_client=github_client,
            jj_client=jj_client,
            label=(labels or {}).get(candidate.change_id),
            trunk_branch=trunk_branch,
        )
        results.append(result)
        if result.retired:
            retired_change_ids.append(candidate.change_id)
    if retired_change_ids and not dry_run:
        current_state = state_store.load()
        next_changes = dict(current_state.changes)
        for change_id in retired_change_ids:
            next_changes.pop(change_id, None)
        state_store.save(current_state.model_copy(update={"changes": next_changes}))
    return tuple(results)


async def _finalize_landed_review(
    *,
    bookmark_policy: BookmarkCleanupPolicy,
    bookmark_state: BookmarkState,
    candidate: LandedReviewCandidate,
    dry_run: bool,
    github_client: GithubClient,
    jj_client: JjClient,
    label: str | None = None,
    trunk_branch: str,
) -> LandedReviewResult:
    if not dry_run:
        rendered_label = (
            t"{label} {ui.change_id(candidate.change_id)}"
            if label is not None
            else t"{ui.change_id(candidate.change_id)}"
        )
        console.output(
            t"Finalizing PR #{candidate.pull_request_number} for {rendered_label}..."
        )
    try:
        pull_request = await github_client.get_pull_request(
            pull_number=candidate.pull_request_number,
        )
    except GithubClientError as error:
        return LandedReviewResult(
            candidate=candidate,
            outcome="skipped",
            skip_reason=t"could not load PR #{candidate.pull_request_number}: {error}",
        )
    pull_request = pull_request.normalize_state()

    if pull_request.state == "open":
        head_mismatch = landed_pull_request_head_mismatch(
            bookmark=candidate.bookmark,
            commit_id=candidate.commit_id,
            github_client=github_client,
            pull_request=pull_request,
        )
        if head_mismatch is not None:
            return LandedReviewResult(
                candidate=candidate,
                outcome="skipped",
                skip_reason=head_mismatch,
            )
        if not dry_run:
            try:
                pull_request = await finalize_landed_pull_request(
                    bookmark=candidate.bookmark,
                    change_id=candidate.change_id,
                    commit_id=candidate.commit_id,
                    github_client=github_client,
                    pull_request=pull_request,
                    pull_request_number=candidate.pull_request_number,
                    trunk_branch=trunk_branch,
                )
            except GithubClientError as error:
                return LandedReviewResult(
                    candidate=candidate,
                    outcome="skipped",
                    skip_reason=t"could not finalize PR "
                    t"#{candidate.pull_request_number}: {error}",
                )
            if pull_request.state == "open":
                return LandedReviewResult(
                    candidate=candidate,
                    outcome="skipped",
                    skip_reason=t"GitHub still reports PR "
                    t"#{candidate.pull_request_number} open after the close request",
                )
        outcome: LandedReviewOutcome = "finalized"
    elif pull_request.state == "merged":
        outcome = "already_merged"
    else:
        return LandedReviewResult(
            candidate=candidate,
            outcome="skipped",
            skip_reason=t"PR #{candidate.pull_request_number} is closed without merge; "
            t"its commit is on {ui.revset('trunk()')} but the review needs an explicit "
            t"decision",
        )

    forget_bookmark = _may_forget_landed_bookmark(
        bookmark_policy=bookmark_policy,
        bookmark_state=bookmark_state,
        candidate=candidate,
    )
    if not dry_run:
        if forget_bookmark:
            jj_client.forget_bookmarks((candidate.bookmark,))
        await delete_landed_stack_comments(
            github_client=github_client,
            pull_request_number=candidate.pull_request_number,
        )
    return LandedReviewResult(
        candidate=candidate,
        outcome=outcome,
        forgot_bookmark=forget_bookmark,
    )


def _may_forget_landed_bookmark(
    *,
    bookmark_policy: BookmarkCleanupPolicy,
    bookmark_state: BookmarkState,
    candidate: LandedReviewCandidate,
) -> bool:
    if not bookmark_policy.cleanup_bookmarks:
        return False
    if not bookmark_cleanup_allowed(
        bookmark=candidate.bookmark,
        bookmark_managed=candidate.bookmark_managed,
        cleanup_user_bookmarks=bookmark_policy.cleanup_user_bookmarks,
        prefix=bookmark_policy.prefix,
    ):
        return False
    return (
        classify_local_bookmark_forget(
            bookmark_state=bookmark_state,
            expected_commit_id=candidate.commit_id,
        )
        == "safe"
    )


def landed_pull_request_head_mismatch(
    *,
    bookmark: str,
    commit_id: str,
    github_client: GithubClient,
    pull_request: GithubPullRequest,
) -> Message | None:
    """Explain why a PR head no longer identifies the landed review, if it doesn't."""

    expected_head_label = f"{github_client.repository.owner}:{bookmark}"
    if (
        pull_request.head.ref == bookmark
        and pull_request.head.label == expected_head_label
        and pull_request.head.sha == commit_id
    ):
        return None
    return (
        t"PR #{pull_request.number} head no longer matches "
        t"{ui.bookmark(expected_head_label)} at commit {ui.commit_id(commit_id)}"
    )


async def finalize_landed_pull_request(
    *,
    bookmark: str,
    change_id: str,
    commit_id: str,
    github_client: GithubClient,
    pull_request: GithubPullRequest | None = None,
    pull_request_number: int,
    trunk_branch: str,
) -> GithubPullRequest:
    """Retarget and close a PR whose exact commit reached trunk directly.

    Raises `GithubClientError` when GitHub refuses; callers decide whether that
    is a skip (the sweep) or a failure. A close rejected because the PR merged
    concurrently is treated as success.
    """

    if pull_request is None:
        pull_request = (
            await github_client.get_pull_request(pull_number=pull_request_number)
        ).normalize_state()
    if pull_request.state == "open":
        if pull_request.base.ref != trunk_branch:
            pull_request = (
                await github_client.update_pull_request(
                    pull_number=pull_request.number,
                    base=trunk_branch,
                    body=pull_request.body or "",
                    title=pull_request.title,
                )
            ).normalize_state()
            mismatch = landed_pull_request_head_mismatch(
                bookmark=bookmark,
                commit_id=commit_id,
                github_client=github_client,
                pull_request=pull_request,
            )
            if mismatch is not None:
                raise GithubClientError(ui.plain_text(mismatch))
    if pull_request.state == "open":
        try:
            await github_client.close_pull_request(pull_number=pull_request.number)
            pull_request = (
                await github_client.get_pull_request(pull_number=pull_request.number)
            ).normalize_state()
        except GithubClientError as error:
            if error.status_code != 422:
                raise
            refreshed = (
                await github_client.get_pull_request(pull_number=pull_request.number)
            ).normalize_state()
            if refreshed.state != "merged":
                raise
            pull_request = refreshed
    return pull_request


async def delete_landed_stack_comments(
    *,
    github_client: GithubClient,
    pull_request_number: int,
) -> None:
    """Delete this PR's managed stack comments, discovered by their markers.

    Comment identity is never cached: managed comments carry body markers and
    are rediscovered on demand. Failures leave harmless residue for the next
    convergence or cleanup pass rather than failing the landing.
    """

    try:
        comments = await github_client.list_issue_comments(
            issue_number=pull_request_number,
        )
    except GithubClientError:
        return
    for kind in ("navigation", "overview"):
        for comment in comments:
            if not _comment_matches_kind(body=comment.body, kind=kind):
                continue
            await delete_stack_comment(
                comment_id=comment.id,
                github_client=github_client,
                kind=kind,
            )


def _comment_matches_kind(*, body: str, kind: StackCommentKind) -> bool:
    if kind == "navigation":
        return is_navigation_comment(body)
    return is_overview_comment(body)


def run_landed_review_sweep(
    *,
    bookmark_policy: BookmarkCleanupPolicy,
    dry_run: bool = False,
    jj_client: JjClient,
    remote_name: str,
    repository: GithubRepoAddress,
    state_store: ReviewStateStore,
    trunk_commit_id: str,
) -> tuple[LandedReviewResult, ...]:
    """Run the sweep from synchronous code, building a GitHub client inside.

    GitHub is contacted only when saved tracking actually has a landed
    candidate, so the common no-op case costs one local ancestry query.
    """

    state = state_store.load()
    if not landed_review_candidates(
        jj_client=jj_client,
        state=state,
        trunk_commit_id=trunk_commit_id,
    ):
        return ()

    async def _run() -> tuple[LandedReviewResult, ...]:
        async with build_github_client(repository=repository) as github_client:
            try:
                repository_state = await github_client.get_repository()
            except GithubClientError:
                return ()
            trunk_branch = resolve_trunk_branch(
                bookmark_states=jj_client.list_bookmark_states(),
                github_repository_state=repository_state,
                remote_name=remote_name,
                trunk_commit_id=trunk_commit_id,
            )
            return await finalize_landed_reviews(
                bookmark_policy=bookmark_policy,
                dry_run=dry_run,
                github_client=github_client,
                jj_client=jj_client,
                state_store=state_store,
                trunk_branch=trunk_branch,
                trunk_commit_id=trunk_commit_id,
            )

    return asyncio.run(_run())
