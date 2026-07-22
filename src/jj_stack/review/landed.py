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
    is_navigation_comment,
    is_overview_comment,
)
from jj_stack.jj.client import JjClient, JjCommandError, UnsupportedStackError
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
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

    change_id: str
    review_identity: ReviewIdentity
    submitted_baseline: SubmittedBaseline


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
    for change_id, review_identity in sorted(state.review_identities.items()):
        if not review_identity.is_tracked:
            continue
        submitted_baseline = state.submitted_baselines.get(change_id)
        if submitted_baseline is None:
            continue
        saved.append(
            LandedReviewCandidate(
                change_id=change_id,
                review_identity=review_identity,
                submitted_baseline=submitted_baseline,
            )
        )
    if not saved:
        return ()
    landed_commit_ids = jj_client.query_commit_ids_ancestors_of(
        tuple(candidate.submitted_baseline.commit_id for candidate in saved),
        descendant_commit_id=trunk_commit_id,
    )
    return tuple(
        candidate
        for candidate in saved
        if candidate.submitted_baseline.commit_id in landed_commit_ids
    )


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
        tuple(candidate.review_identity.head_ref for candidate in candidates)
    )
    results: list[LandedReviewResult] = []
    for candidate in candidates:
        result = await _finalize_landed_review(
            bookmark_policy=bookmark_policy,
            bookmark_state=bookmark_states.get(
                candidate.review_identity.head_ref,
                BookmarkState(name=candidate.review_identity.head_ref),
            ),
            candidate=candidate,
            dry_run=dry_run,
            github_client=github_client,
            jj_client=jj_client,
            label=(labels or {}).get(candidate.change_id),
            trunk_branch=trunk_branch,
        )
        results.append(result)
        if result.retired and not dry_run:
            state_store.retire_review(
                candidate.change_id,
                expected_identity=candidate.review_identity,
                expected_baseline=candidate.submitted_baseline,
            )
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
    # The same rule the rebase path applies: a landed change carrying local
    # edits since its last submit is stopped and reported, never retired, so
    # the linkage its own recovery guidance depends on stays intact.
    local_edits = _local_edits_skip_reason(candidate=candidate, jj_client=jj_client)
    if local_edits is not None:
        return LandedReviewResult(
            candidate=candidate,
            outcome="skipped",
            skip_reason=local_edits,
        )
    if not dry_run:
        rendered_label = (
            t"{label} {ui.change_id(candidate.change_id)}"
            if label is not None
            else t"{ui.change_id(candidate.change_id)}"
        )
        console.output(
            t"Finalizing PR #{candidate.review_identity.pr_number} for {rendered_label}..."
        )
    try:
        pull_request = await github_client.get_pull_request(
            pull_number=candidate.review_identity.pr_number,
        )
    except GithubClientError as error:
        return LandedReviewResult(
            candidate=candidate,
            outcome="skipped",
            skip_reason=t"could not load PR #{candidate.review_identity.pr_number}: {error}",
        )
    pull_request = pull_request.normalize_state()

    # Every mutation path — finalizing an open PR or tearing down a merged
    # one — requires the head to still identify this exact review.
    head_mismatch = landed_pull_request_head_mismatch(
        bookmark=candidate.review_identity.head_ref,
        commit_id=candidate.submitted_baseline.commit_id,
        github_client=github_client,
        head_owner=candidate.review_identity.head_owner,
        pull_request=pull_request,
    )
    if head_mismatch is not None:
        return LandedReviewResult(
            candidate=candidate,
            outcome="skipped",
            skip_reason=head_mismatch,
        )

    if pull_request.state == "open":
        if not dry_run:
            try:
                pull_request = await finalize_landed_pull_request(
                    bookmark=candidate.review_identity.head_ref,
                    change_id=candidate.change_id,
                    commit_id=candidate.submitted_baseline.commit_id,
                    github_client=github_client,
                    head_owner=candidate.review_identity.head_owner,
                    pull_request=pull_request,
                    pull_request_number=candidate.review_identity.pr_number,
                    trunk_branch=trunk_branch,
                )
            except GithubClientError as error:
                return LandedReviewResult(
                    candidate=candidate,
                    outcome="skipped",
                    skip_reason=t"could not finalize PR "
                    t"#{candidate.review_identity.pr_number}: {error}",
                )
            if pull_request.state == "open":
                return LandedReviewResult(
                    candidate=candidate,
                    outcome="skipped",
                    skip_reason=t"GitHub still reports PR "
                    t"#{candidate.review_identity.pr_number} open after the close request",
                )
        outcome: LandedReviewOutcome = "finalized"
    elif pull_request.state == "merged":
        outcome = "already_merged"
    else:
        retire_command = f"unstack --cleanup --pull-request {candidate.review_identity.pr_number}"
        return LandedReviewResult(
            candidate=candidate,
            outcome="skipped",
            skip_reason=t"PR #{candidate.review_identity.pr_number} is closed without merge "
            t"although its commit is on {ui.revset('trunk()')}; reopen it, or retire "
            t"the review with {ui.cmd(retire_command)}",
        )

    forget_bookmark = _may_forget_landed_bookmark(
        bookmark_policy=bookmark_policy,
        bookmark_state=bookmark_state,
        candidate=candidate,
    )
    if not dry_run:
        if forget_bookmark:
            jj_client.forget_bookmarks((candidate.review_identity.head_ref,))
        await delete_landed_stack_comments(
            github_client=github_client,
            pull_request_number=candidate.review_identity.pr_number,
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
        bookmark=candidate.review_identity.head_ref,
        bookmark_managed=candidate.review_identity.manages_bookmark,
        cleanup_user_bookmarks=bookmark_policy.cleanup_user_bookmarks,
        prefix=bookmark_policy.prefix,
    ):
        return False
    return (
        classify_local_bookmark_forget(
            bookmark_state=bookmark_state,
            expected_commit_id=candidate.submitted_baseline.commit_id,
        )
        == "safe"
    )


def _local_edits_skip_reason(
    *,
    candidate: LandedReviewCandidate,
    jj_client: JjClient,
) -> Message | None:
    try:
        live_commit_id = jj_client.resolve_revision(candidate.change_id).commit_id
    except JjCommandError, UnsupportedStackError:
        return (
            t"{ui.change_id(candidate.change_id)} does not resolve to one local "
            t"revision; inspect it with {ui.cmd('view --fetch')}"
        )
    if live_commit_id == candidate.submitted_baseline.commit_id:
        return None
    return (
        t"{ui.change_id(candidate.change_id)} has local edits since its last "
        t"submit; push a new version first or rebase manually"
    )


def landed_pull_request_head_mismatch(
    *,
    bookmark: str,
    commit_id: str,
    github_client: GithubClient,
    head_owner: str | None = None,
    pull_request: GithubPullRequest,
) -> Message | None:
    """Explain why a PR head no longer identifies the landed review, if it doesn't."""

    expected_head_label = f"{head_owner or github_client.repository.owner}:{bookmark}"
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
    head_owner: str | None = None,
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
                head_owner=head_owner,
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
        matches = [
            comment for comment in comments if _comment_matches_kind(body=comment.body, kind=kind)
        ]
        # The same multiplicity stance as submit and cleanup: more than one
        # marker match is ambiguous, so leave the residue for inspection.
        if len(matches) != 1:
            continue
        try:
            await github_client.delete_issue_comment(comment_id=matches[0].id)
        except GithubClientError:
            continue


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
