"""Predict and detect GitHub's reachability-based pull request auto-close."""

from __future__ import annotations

import jj_review.ui as ui
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.jj.client import JjClient
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.github import GithubPullRequest

from .models import PendingPullRequestSync, PreparedSubmitRevision


async def retarget_review_bases_before_branch_push(
    *,
    bookmark_states: dict[str, BookmarkState],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    jj_client: JjClient,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote_name: str,
    trunk_branch: str,
) -> None:
    """Move PR bases that would auto-close after the push to trunk first."""

    retarget_syncs = predict_pull_requests_auto_closed_by_push(
        bookmark_states=bookmark_states,
        jj_client=jj_client,
        pending_syncs=pending_syncs,
        prepared_revisions=prepared_revisions,
        remote_name=remote_name,
    )
    await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=retarget_syncs,
        run_item=lambda pending_sync: _retarget_review_base_before_branch_push(
            github_client=github_client,
            github_repository=github_repository,
            pending_sync=pending_sync,
            trunk_branch=trunk_branch,
        ),
    )


def predict_pull_requests_auto_closed_by_push(
    *,
    bookmark_states: dict[str, BookmarkState],
    jj_client: JjClient,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote_name: str,
) -> tuple[PendingPullRequestSync, ...]:
    """Pending PRs that GitHub will auto-close (as merged) after the planned push.

    GitHub auto-closes an open PR when its head ref becomes contained in its base
    ref. The push moves head and (transitively, via stacked bookmarks) base, so
    the prediction is run against the post-push commit IDs each ref will hold.
    """

    push_targets = {
        prepared_revision.bookmark: prepared_revision.revision.commit_id
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
            bookmark_states=bookmark_states,
            remote_name=remote_name,
        )
        if base_after_push is None:
            continue
        candidates.append((head_after_push, base_after_push, pending_sync))

    if not candidates:
        return ()
    auto_close_heads = jj_client.query_paired_ancestor_membership(
        tuple((head, base) for head, base, _ in candidates),
    )
    return tuple(
        pending_sync
        for head, _, pending_sync in candidates
        if head in auto_close_heads
    )


def _resolve_post_push_commit(
    *,
    bookmark_states: dict[str, BookmarkState],
    push_targets: dict[str, str],
    ref: str,
    remote_name: str,
) -> str | None:
    """Resolve the commit ID a ref will point at after the planned push lands."""

    if ref in push_targets:
        return push_targets[ref]
    bookmark_state = bookmark_states.get(ref)
    if bookmark_state is None:
        return None
    remote_state = bookmark_state.remote_target(remote_name)
    if remote_state is None or remote_state.target is None:
        return None
    return remote_state.target


async def _retarget_review_base_before_branch_push(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pending_sync: PendingPullRequestSync,
    trunk_branch: str,
) -> None:
    pull_request = pending_sync.discovered_pull_request
    if pull_request is None:
        raise AssertionError("Pre-push retarget requires a discovered pull request.")
    try:
        await github_client.update_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request.number,
            base=trunk_branch,
            body=pull_request.body or "",
            title=pull_request.title,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not retarget PR #{pull_request.number} to "
            t"{ui.bookmark(trunk_branch)} before pushing review branches"
        ) from error


async def verify_no_unexpected_pull_request_closures(
    *,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
) -> None:
    """Detect open→closed and open→missing transitions and raise loudly.

    `submit` never closes pull requests on purpose. Any PR that was open at the
    start of this run and is no longer open by the end means a GitHub destructive
    default fired in a way the pre-push predictor did not anticipate (typically
    the head-contained-in-base auto-close) or the PR vanished entirely (deleted
    or transferred). Surface either case loudly rather than persist state that
    hides the loss.
    """

    initially_open_numbers = sorted(
        {
            pull_request.number
            for pull_request in discovered_pull_requests.values()
            if pull_request is not None and pull_request.state == "open"
        }
    )
    if not initially_open_numbers:
        return
    try:
        refetched = await github_client.get_pull_requests_by_numbers(
            github_repository.owner,
            github_repository.repo,
            pull_numbers=initially_open_numbers,
        )
    except GithubClientError as error:
        raise CliError(
            "Could not refetch pull request states for the post-submit safety check"
        ) from error

    closed_numbers: list[int] = []
    missing_numbers: list[int] = []
    for number in initially_open_numbers:
        pull_request = refetched.get(number)
        if pull_request is None:
            missing_numbers.append(number)
            continue
        if pull_request.state != "open":
            closed_numbers.append(number)
    if not closed_numbers and not missing_numbers:
        return

    message_parts: list[ui.Message] = []
    if closed_numbers:
        closed_rendered = ", ".join(f"#{number}" for number in closed_numbers)
        message_parts.append(
            t"Pull request(s) {closed_rendered} were open at the start of this submit "
            t"but are closed by the end. GitHub closes a pull request automatically "
            t"when its head commit becomes reachable from the base branch during a "
            t"push. Reopen those pull requests on GitHub now to keep their existing "
            t"reviews."
        )
    if missing_numbers:
        missing_rendered = ", ".join(f"#{number}" for number in missing_numbers)
        if message_parts:
            message_parts.append(" ")
        message_parts.append(
            t"Pull request(s) {missing_rendered} were open at the start of this "
            t"submit but GitHub no longer reports them. Inspect those pull requests "
            t"on GitHub to see whether they were deleted or transferred."
        )
    message_parts.append(" ")
    message_parts.append(
        t"Once the affected pull requests are restored, rerunning "
        t"{ui.cmd('jj-review submit')} is safe and will restore the stacked bases."
    )
    raise CliError(tuple(message_parts))
