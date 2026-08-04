"""Prevent GitHub's reachability-based pull request auto-close."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient

from .models import PendingPullRequestSync, PreparedSubmitRevision


async def retarget_review_bases_before_branch_push(
    *,
    github_client: GithubClient,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    trunk_branch: str,
) -> None:
    """Move PR bases that would auto-close after the push to trunk first."""

    await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=pending_syncs,
        run_item=lambda pending_sync: _retarget_review_base_before_branch_push(
            github_client=github_client,
            pending_sync=pending_sync,
            trunk_branch=trunk_branch,
        ),
    )


def predict_pull_requests_auto_closed_by_push(
    *,
    jj_client: JjClient,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote_targets: dict[str, str],
) -> tuple[PendingPullRequestSync, ...]:
    """Pending PRs that GitHub will auto-close (as merged) after the planned push.

    GitHub auto-closes an open PR when its head ref becomes contained in its base
    ref. The push moves head and, transitively through the planned stacked branch
    updates, base, so the prediction uses the post-push commit IDs each ref will
    hold.
    """

    push_targets = {
        prepared_revision.branch: prepared_revision.revision.commit_id
        for prepared_revision in prepared_revisions
    }
    candidates: list[tuple[str, str, PendingPullRequestSync]] = []
    for pending_sync in pending_syncs:
        pull_request = pending_sync.discovered_pull_request
        if pull_request is None or pull_request.state != "open":
            continue
        head_after_push = push_targets.get(pull_request.head.ref)
        if head_after_push is None:
            continue
        base_after_push = _resolve_post_push_commit(
            ref=pull_request.base.ref,
            push_targets=push_targets,
            remote_targets=remote_targets,
        )
        if base_after_push is None:
            continue
        candidates.append((head_after_push, base_after_push, pending_sync))

    if not candidates:
        return ()
    auto_close_heads = jj_client.query_paired_ancestor_membership(
        tuple((head, base) for head, base, _ in candidates),
    )
    return tuple(pending_sync for head, _, pending_sync in candidates if head in auto_close_heads)


def _resolve_post_push_commit(
    *,
    push_targets: dict[str, str],
    ref: str,
    remote_targets: dict[str, str],
) -> str | None:
    """Resolve the commit ID a ref will point at after the planned push lands."""

    if ref in push_targets:
        return push_targets[ref]
    return remote_targets.get(ref)


async def _retarget_review_base_before_branch_push(
    *,
    github_client: GithubClient,
    pending_sync: PendingPullRequestSync,
    trunk_branch: str,
) -> None:
    pull_request = pending_sync.discovered_pull_request
    if pull_request is None:
        raise AssertionError("Pre-push retarget requires a discovered pull request.")
    try:
        await github_client.update_pull_request(
            pull_number=pull_request.number,
            base=trunk_branch,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not retarget PR #{pull_request.number} to "
            t"{ui.bookmark(trunk_branch)} before pushing review branches"
        ) from error
