"""Sync pull request state on GitHub for each prepared revision."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import jj_review.ui as ui
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.models.github import GithubPullRequest, GithubPullRequestReview
from jj_review.models.review_state import CachedChange
from jj_review.review.bookmarks import BookmarkSource, bookmark_ownership_for_source
from jj_review.review.change_status import classify_saved_review_change

from .models import (
    PendingPullRequestSync,
    PullRequestAction,
    ResolvedSubmitOptions,
    SubmitMutationRun,
    SubmitOptions,
    SubmittedRevision,
)
from .revisions import ensure_change_is_not_unlinked


async def discover_pull_requests_by_bookmark(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    bookmarks: tuple[str, ...],
) -> dict[str, GithubPullRequest | None]:
    if not bookmarks:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=bookmarks,
        )
    except GithubClientError as error:
        raise CliError("Could not batch pull request discovery for branches") from error

    return {
        bookmark: _select_discovered_pull_request(
            head_label=f"{github_repository.owner}:{bookmark}",
            pull_requests=discovered_pull_requests.get(bookmark, ()),
        )
        for bookmark in bookmarks
    }


async def sync_pull_requests(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    options: SubmitOptions,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    resolved_options: ResolvedSubmitOptions,
    run: SubmitMutationRun,
    on_progress: Callable[[], None] | None = None,
) -> tuple[SubmittedRevision, ...]:
    def handle_success(
        _index: int,
        submitted: tuple[SubmittedRevision, CachedChange | None],
    ) -> None:
        submitted_revision, cached_change = submitted
        previous_change = (
            run.state_changes.get(submitted_revision.change_id)
            or run.state.changes.get(submitted_revision.change_id)
        )
        if cached_change is not None:
            run.state_changes[submitted_revision.change_id] = cached_change
            run.record_saved_state_update(
                after=cached_change,
                before=previous_change,
                change_id=submitted_revision.change_id,
            )
        run.save_interim_state()
        if on_progress is not None:
            on_progress()

    submitted_revisions = await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=pending_syncs,
        run_item=lambda pending_sync: _sync_pull_request(
            github_client=github_client,
            github_repository=github_repository,
            options=options,
            pending_sync=pending_sync,
            resolved_options=resolved_options,
            run=run,
        ),
        on_success=handle_success,
    )
    return tuple(submitted_revision for submitted_revision, _ in submitted_revisions)


async def _sync_pull_request(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    options: SubmitOptions,
    pending_sync: PendingPullRequestSync,
    resolved_options: ResolvedSubmitOptions,
    run: SubmitMutationRun,
) -> tuple[SubmittedRevision, CachedChange | None]:
    prepared_revision = pending_sync.prepared_revision
    bookmark = prepared_revision.bookmark
    change_id = prepared_revision.change_id
    discovered_pull_request = pending_sync.discovered_pull_request
    cached_change = run.state.changes.get(change_id)
    saved_status = classify_saved_review_change(cached_change, local="present")
    _ensure_pull_request_link_is_consistent(
        bookmark=bookmark,
        cached_change=cached_change,
        change_id=change_id,
        discovered_pull_request=discovered_pull_request,
    )

    title = pending_sync.generated_description.title
    body = pending_sync.generated_description.body
    if discovered_pull_request is None:
        pull_request = None
        if not run.dry_run:
            pull_request = await _create_pull_request(
                base_branch=pending_sync.base_branch,
                body=body,
                draft=(options.draft_mode in ("draft", "draft_all")),
                github_client=github_client,
                github_repository=github_repository,
                head_branch=bookmark,
                title=title,
            )
        action: PullRequestAction = "created"
    elif (
        discovered_pull_request.base.ref == pending_sync.base_branch
        and (discovered_pull_request.body or "") == body
        and discovered_pull_request.title == title
    ):
        pull_request = discovered_pull_request
        action = "unchanged"
    else:
        pull_request = discovered_pull_request
        if not run.dry_run:
            pull_request = await _update_pull_request(
                base_branch=pending_sync.base_branch,
                body=body,
                github_client=github_client,
                github_repository=github_repository,
                pull_request=discovered_pull_request,
                title=title,
            )
        action = "updated"

    if pull_request is not None and pull_request.state == "open":
        if options.draft_mode == "publish" and pull_request.is_draft:
            if not run.dry_run:
                pull_request = await _mark_pull_request_ready_for_review(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"
        elif options.draft_mode == "draft_all" and not pull_request.is_draft:
            if not run.dry_run:
                pull_request = await _convert_pull_request_to_draft(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"

    if (
        not run.dry_run
        and pull_request is not None
        and (
            action != "unchanged"
            or not saved_status.saved_pull_request_identity
        )
    ):
        await _sync_pull_request_metadata(
            github_client=github_client,
            github_repository=github_repository,
            labels=resolved_options.labels,
            pull_request_number=pull_request.number,
            reviewers=resolved_options.reviewers,
            team_reviewers=resolved_options.team_reviewers,
        )

    if not run.dry_run and options.re_request and pull_request is not None:
        re_request_reviewers = await _load_re_request_reviewers(
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pull_request.number,
        )
        merged_reviewers = _merge_re_request_reviewers(
            reviewers=resolved_options.reviewers,
            re_request_reviewers=re_request_reviewers,
        )
        if merged_reviewers != resolved_options.reviewers:
            await _sync_pull_request_metadata(
                github_client=github_client,
                github_repository=github_repository,
                labels=[],
                pull_request_number=pull_request.number,
                reviewers=merged_reviewers,
                team_reviewers=[],
            )

    next_cached_change: CachedChange | None = None
    if pull_request is not None:
        next_cached_change = _updated_cached_change(
            bookmark=bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            cached_change=cached_change,
            commit_id=prepared_revision.revision.commit_id,
            parent_change_id=pending_sync.parent_change_id,
            pull_request=pull_request,
            stack_head_change_id=pending_sync.stack_head_change_id,
        )
    return (
        SubmittedRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            change_id=prepared_revision.change_id,
            commit_id=prepared_revision.revision.commit_id,
            local_action=prepared_revision.local_action,
            native_revision=prepared_revision.revision,
            pull_request_action=action,
            pull_request_is_draft=(
                pull_request.is_draft if pull_request is not None else None
            ),
            pull_request_number=(
                pull_request.number if pull_request is not None else None
            ),
            pull_request_title=(
                pull_request.title if pull_request is not None else None
            ),
            pull_request_url=(
                pull_request.html_url if pull_request is not None else None
            ),
            remote_action=prepared_revision.remote_action,
            subject=prepared_revision.revision.subject,
        ),
        next_cached_change,
    )


async def _load_re_request_reviewers(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
) -> list[str]:
    try:
        reviews = await github_client.list_pull_request_reviews(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not load reviews for pull request #{pull_request_number}"
        ) from error
    return _reviewers_to_re_request(reviews)


def _reviewers_to_re_request(
    reviews: Sequence[GithubPullRequestReview],
) -> list[str]:
    latest_reviews_by_user: dict[str, GithubPullRequestReview] = {}
    for review in sorted(reviews, key=lambda item: item.id):
        reviewer = review.user
        if reviewer is None:
            continue
        normalized_state = review.state.upper()
        if normalized_state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        latest_reviews_by_user[reviewer.login] = review

    selected_reviews = sorted(
        (
            review
            for review in latest_reviews_by_user.values()
            if review.state.upper() in {"APPROVED", "CHANGES_REQUESTED"}
        ),
        key=lambda item: item.id,
    )
    return [review.user.login for review in selected_reviews if review.user is not None]


def _merge_re_request_reviewers(
    *,
    reviewers: list[str],
    re_request_reviewers: list[str],
) -> list[str]:
    merged = list(reviewers)
    seen = set(reviewers)
    for reviewer in re_request_reviewers:
        if reviewer in seen:
            continue
        seen.add(reviewer)
        merged.append(reviewer)
    return merged


def _select_discovered_pull_request(
    *,
    head_label: str,
    pull_requests: tuple[GithubPullRequest, ...],
) -> GithubPullRequest | None:
    if len(pull_requests) > 1:
        raise CliError(
            t"GitHub reports multiple pull requests for head branch "
            t"{ui.bookmark(head_label)}.",
            hint=(
                t"Inspect the PR link with {ui.cmd('view --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if not pull_requests:
        return None
    pull_request = pull_requests[0]
    if pull_request.state != "open":
        raise CliError(
            t"GitHub reports pull request #{pull_request.number} for head branch "
            t"{ui.bookmark(head_label)} in state {pull_request.state}.",
            hint=(
                t"Inspect the PR link with {ui.cmd('view --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    return pull_request


def _ensure_pull_request_link_is_consistent(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
) -> None:
    ensure_change_is_not_unlinked(
        cached_change=cached_change,
        change_id=change_id,
    )
    review_status = classify_saved_review_change(cached_change, local="present")
    if not review_status.saved_pull_request_identity:
        return
    if cached_change is None:
        raise AssertionError("Saved pull request identity requires cached state.")
    if discovered_pull_request is None:
        raise CliError(
            t"Saved pull request link exists for bookmark {ui.bookmark(bookmark)}, "
            t"but GitHub no longer reports a PR for that head branch.",
            hint=(
                t"Inspect the PR link with {ui.cmd('view --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if cached_change.pr_number not in (None, discovered_pull_request.number):
        raise CliError(
            t"Saved pull request #{cached_change.pr_number} does not match the PR "
            t"GitHub reports for bookmark {ui.bookmark(bookmark)} "
            t"(#{discovered_pull_request.number}).",
            hint=(
                t"Inspect the PR link with {ui.cmd('view --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if cached_change.pr_url not in (None, discovered_pull_request.html_url):
        raise CliError(
            t"Saved pull request URL for bookmark {ui.bookmark(bookmark)} does not "
            t"match GitHub.",
            hint=(
                t"Inspect the PR link with {ui.cmd('view --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )


async def _create_pull_request(
    *,
    base_branch: str,
    body: str,
    draft: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    head_branch: str,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.create_pull_request(
            github_repository.owner,
            github_repository.repo,
            base=base_branch,
            body=body,
            draft=draft,
            head=head_branch,
            title=title,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not create a pull request for branch {ui.bookmark(head_branch)}"
        ) from error


async def _sync_pull_request_metadata(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    pull_request_number: int,
    reviewers: list[str],
    team_reviewers: list[str],
) -> None:
    try:
        if reviewers or team_reviewers:
            await github_client.request_reviewers(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if labels:
            await github_client.add_labels(
                github_repository.owner,
                github_repository.repo,
                issue_number=pull_request_number,
                labels=labels,
            )
    except GithubClientError as error:
        raise CliError(
            f"Could not synchronize metadata for pull request #{pull_request_number}"
        ) from error


async def _mark_pull_request_ready_for_review(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.mark_pull_request_ready_for_review(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}"
        ) from error


async def _convert_pull_request_to_draft(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.convert_pull_request_to_draft(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}"
        ) from error


async def _update_pull_request(
    *,
    base_branch: str,
    body: str,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.update_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request.number,
            base=base_branch,
            body=body,
            title=title,
        )
    except GithubClientError as error:
        raise CliError(f"Could not update pull request #{pull_request.number}") from error


def _updated_cached_change(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    cached_change: CachedChange | None,
    commit_id: str,
    parent_change_id: str | None,
    pull_request: GithubPullRequest,
    stack_head_change_id: str | None,
) -> CachedChange:
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            bookmark_ownership=bookmark_ownership_for_source(bookmark_source),
            last_submitted_commit_id=commit_id,
            last_submitted_parent_change_id=parent_change_id,
            last_submitted_stack_head_change_id=stack_head_change_id,
            pr_is_draft=pull_request.is_draft,
            pr_number=pull_request.number,
            pr_state=pull_request.state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": bookmark_ownership_for_source(bookmark_source),
            "last_submitted_commit_id": commit_id,
            "last_submitted_parent_change_id": parent_change_id,
            "last_submitted_stack_head_change_id": stack_head_change_id,
            "pr_is_draft": pull_request.is_draft,
            "pr_number": pull_request.number,
            "pr_state": pull_request.state,
            "pr_url": pull_request.html_url,
        }
    )
