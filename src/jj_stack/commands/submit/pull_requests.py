"""Sync pull request state on GitHub for each prepared revision."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

import jj_stack.ui as ui
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError, DriftError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubPullRequest, GithubPullRequestReview
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
    TrackedReview,
)
from jj_stack.ui import Message

from .models import (
    PreparedSubmitRevision,
    PullRequestDraftAction,
    PullRequestSyncPlan,
    SubmitMutationRun,
    SubmittedRevision,
)


async def _github_request[Result](
    request: Awaitable[Result],
    *,
    error_message: Message,
) -> Result:
    try:
        return await request
    except GithubClientError as error:
        raise CliError(error_message) from error


async def discover_pull_requests_by_branch(
    *,
    github_client: GithubClient,
    branches: tuple[str, ...],
    tracked_pull_requests: Mapping[str, int],
) -> dict[str, GithubPullRequest | None]:
    if not branches:
        return {}

    discovered_pull_requests = await _github_request(
        github_client.get_pull_requests_by_head_refs(head_refs=branches),
        error_message="Could not batch pull request discovery for branches",
    )

    return {
        branch: _select_discovered_pull_request(
            head_label=f"{github_client.repository.owner}:{branch}",
            pull_requests=discovered_pull_requests.get(branch, ()),
            tracked_pull_number=tracked_pull_requests.get(branch),
        )
        for branch in branches
    }


async def load_re_request_reviewers(
    *,
    github_client: GithubClient,
    pull_requests: tuple[GithubPullRequest, ...],
) -> dict[int, list[str]]:
    reviews = await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=pull_requests,
        run_item=lambda pull_request: _github_request(
            github_client.list_pull_request_reviews(pull_number=pull_request.number),
            error_message=f"Could not load reviews for pull request #{pull_request.number}",
        ),
    )
    return {
        pull_request.number: _reviewers_to_re_request(pull_request_reviews)
        for pull_request, pull_request_reviews in zip(pull_requests, reviews, strict=True)
    }


def ensure_pull_request_syncs_are_safe(
    *,
    discovered_pull_requests: Mapping[str, GithubPullRequest | None],
    existing_only: bool,
    prepared_revisions: Sequence[PreparedSubmitRevision],
    repository_key: tuple[str, str],
    state: ReviewState,
) -> None:
    """Verify every planned PR sync before any mutation.

    A damaged or divergent link anywhere in the plan must stop `submit` before
    review branches push or sibling PRs sync. Validating per change inside the
    concurrent sync phase would let a mid-stack link failure surface only after
    those mutations have already happened.
    """

    for prepared_revision in prepared_revisions:
        change_id = prepared_revision.revision.change_id
        tracked_review = state.tracked_review(change_id)
        pull_request = discovered_pull_requests[prepared_revision.branch]
        if pull_request is not None and pull_request.is_queued:
            head_change_id = prepared_revisions[-1].revision.change_id
            raise CliError(
                t"PR #{pull_request.number} for {ui.change_id(change_id)} is in the merge "
                t"queue, so submit made no changes. Any new changes above it remain "
                t"unsubmitted.",
                hint=t"Wait for the queued changes to merge, then run "
                t"{ui.cmd(f'jj-stack sync {head_change_id}')} followed by "
                t"{ui.cmd(f'jj-stack submit {head_change_id}')}.",
            )
        ensure_pull_request_link_is_consistent(
            branch=prepared_revision.branch,
            change_id=change_id,
            discovered_pull_request=pull_request,
            expected_remote_target=prepared_revision.expected_remote_target,
            repository_key=repository_key,
            tracked_review=tracked_review,
        )
        if existing_only and (tracked_review is None or pull_request is None):
            raise CliError(
                t"Cannot sync {ui.change_id(change_id)} without its existing pull request.",
                hint=t"Repair the review link with {ui.cmd('relink')} before retrying.",
            )


async def sync_pull_requests(
    *,
    github_client: GithubClient,
    plans: tuple[PullRequestSyncPlan, ...],
    run: SubmitMutationRun,
    on_progress: Callable[[], None] | None = None,
) -> tuple[SubmittedRevision, ...]:
    def handle_success(
        _index: int,
        submitted: tuple[
            SubmittedRevision,
            ReviewIdentity | None,
            SubmittedBaseline | None,
        ],
    ) -> None:
        submitted_revision, identity, baseline = submitted
        if identity is not None and baseline is not None:
            run.record_submission(
                baseline=baseline,
                change_id=submitted_revision.change_id,
                identity=identity,
            )
        if on_progress is not None:
            on_progress()

    submitted_revisions = await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=plans,
        run_item=lambda plan: _sync_pull_request(
            github_client=github_client,
            plan=plan,
            run=run,
        ),
        on_success=handle_success,
    )
    return tuple(submitted_revision for submitted_revision, _, _ in submitted_revisions)


async def _sync_pull_request(
    *,
    github_client: GithubClient,
    plan: PullRequestSyncPlan,
    run: SubmitMutationRun,
) -> tuple[SubmittedRevision, ReviewIdentity | None, SubmittedBaseline | None]:
    prepared_revision = plan.prepared
    branch = prepared_revision.branch
    change_id = prepared_revision.revision.change_id
    pull_request = plan.discovered_pull_request
    review_identity = run.state.review_identities.get(change_id)
    action = plan.action
    base_update, body_update, title_update = plan.content_updates

    if action == "created":
        if not run.dry_run:
            pull_request = await _github_request(
                github_client.create_pull_request(
                    base=plan.base_branch,
                    body=plan.generated_description.body,
                    draft=plan.draft,
                    head=branch,
                    title=plan.generated_description.title,
                ),
                error_message=t"Could not create a pull request for branch {ui.bookmark(branch)}",
            )
    elif (
        any(update is not None for update in (base_update, body_update, title_update))
        and not run.dry_run
    ):
        assert pull_request is not None
        pull_request = await _github_request(
            github_client.update_pull_request(
                pull_number=pull_request.number,
                base=base_update,
                body=body_update,
                title=title_update,
            ),
            error_message=f"Could not update pull request #{pull_request.number}",
        )

    if pull_request is not None and not run.dry_run:
        pull_request = await _apply_draft_action(
            action=plan.draft_action,
            github_client=github_client,
            pull_request=pull_request,
        )

    if not run.dry_run and pull_request is not None and plan.metadata is not None:
        await _sync_pull_request_metadata(
            github_client=github_client,
            labels=plan.metadata.labels,
            pull_request_number=pull_request.number,
            reviewers=plan.metadata.reviewers,
            team_reviewers=plan.metadata.team_reviewers,
        )

    next_identity: ReviewIdentity | None = None
    next_baseline: SubmittedBaseline | None = None
    if pull_request is not None:
        next_identity = _submitted_identity(
            branch=branch,
            github_client=github_client,
            pull_request=pull_request,
            review_identity=review_identity,
        )
        next_baseline = SubmittedBaseline(commit_id=prepared_revision.revision.commit_id)
    return (
        SubmittedRevision(
            prepared=prepared_revision,
            pull_request_action=action,
            pull_request_is_draft=(pull_request.is_draft if pull_request is not None else None),
            pull_request_number=(pull_request.number if pull_request is not None else None),
            pull_request_url=(pull_request.html_url if pull_request is not None else None),
        ),
        next_identity,
        next_baseline,
    )


async def _apply_draft_action(
    *,
    action: PullRequestDraftAction | None,
    github_client: GithubClient,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if action is None:
        return pull_request
    message = (
        f"Could not return pull request #{pull_request.number} to draft for "
        f"{github_client.repository.full_name}"
        if action == "draft"
        else f"Could not mark draft pull request #{pull_request.number} ready for review for "
        f"{github_client.repository.full_name}"
    )
    if pull_request.node_id is None:
        raise CliError(f"{message}: GitHub did not return a node ID.")
    request = (
        github_client.convert_pull_request_to_draft(pull_request_id=pull_request.node_id)
        if action == "draft"
        else github_client.mark_pull_request_ready_for_review(
            pull_request_id=pull_request.node_id
        )
    )
    return await _github_request(request, error_message=message)


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


def _select_discovered_pull_request(
    *,
    head_label: str,
    pull_requests: tuple[GithubPullRequest, ...],
    tracked_pull_number: int | None,
) -> GithubPullRequest | None:
    open_pull_requests = tuple(
        pull_request for pull_request in pull_requests if pull_request.state == "open"
    )
    tracked_pull_request = next(
        (
            pull_request
            for pull_request in pull_requests
            if pull_request.number == tracked_pull_number
        ),
        None,
    )
    if len(open_pull_requests) > 1 or (
        open_pull_requests
        and tracked_pull_request is not None
        and open_pull_requests[0].number != tracked_pull_request.number
    ):
        raise DriftError(
            t"GitHub reports multiple pull requests for head branch {ui.bookmark(head_label)}.",
            condition="pull_request_ambiguous",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    return tracked_pull_request or (open_pull_requests[0] if open_pull_requests else None)


def ensure_pull_request_link_is_consistent(
    *,
    branch: str,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
    expected_remote_target: str | None,
    repository_key: tuple[str, str],
    tracked_review: TrackedReview | None,
    merged_hint: Message | None = None,
) -> None:
    if tracked_review is None:
        if discovered_pull_request is not None:
            raise DriftError(
                t"GitHub already reports PR #{discovered_pull_request.number} for "
                t"untracked branch {ui.bookmark(branch)}.",
                condition="saved_pull_request_missing",
                hint=t"Adopt that PR explicitly with {ui.cmd('relink')} before submitting.",
            )
        return
    review_identity = tracked_review.review_identity
    if review_identity.repository_key != repository_key:
        raise DriftError(
            t"Saved PR tracking for {ui.change_id(change_id)} belongs to a different GitHub "
            t"repository than the one this remote resolves to.",
            condition="saved_pull_request_mismatch",
            hint=(
                t"Point the remote back at that repository, or reattach the change with "
                t"{ui.cmd('relink')} before submitting again."
            ),
        )
    if review_identity.head_ref != branch:
        raise DriftError(
            t"Saved PR tracking for {ui.change_id(change_id)} names branch "
            t"{ui.bookmark(review_identity.head_ref)}, not {ui.bookmark(branch)}.",
            condition="saved_pull_request_mismatch",
            hint=t"Run {ui.cmd('relink')} before submitting again.",
        )
    if discovered_pull_request is None:
        raise DriftError(
            t"Saved pull request link exists for branch {ui.bookmark(branch)}, "
            t"but GitHub no longer reports a PR for that head branch.",
            condition="saved_pull_request_missing",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    discovered_pull_request = discovered_pull_request.normalize_state()
    if review_identity.pr_number != discovered_pull_request.number:
        raise DriftError(
            t"Saved pull request #{review_identity.pr_number} does not match the PR "
            t"GitHub reports for branch {ui.bookmark(branch)} "
            t"(#{discovered_pull_request.number}).",
            condition="saved_pull_request_mismatch",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if not review_identity.matches_pull_request(discovered_pull_request):
        raise DriftError(
            t"Saved pull request #{review_identity.pr_number} no longer has the exact "
            t"saved head owner and branch.",
            condition="saved_pull_request_mismatch",
            hint=t"Inspect it, then repair the intended review with {ui.cmd('relink')}.",
        )
    if discovered_pull_request.state != "open":
        hint = (
            merged_hint
            if discovered_pull_request.state == "merged" and merged_hint is not None
            else t"Run {ui.cmd(f'jj-stack sync {change_id}')} to update the local stack."
            if discovered_pull_request.state == "merged"
            else t"Reopen the PR, or run {ui.cmd(f'jj-stack cleanup {change_id}')} before "
            t"starting a new review."
        )
        raise DriftError(
            t"PR #{discovered_pull_request.number} for {ui.change_id(change_id)} is "
            t"{discovered_pull_request.state} and cannot be updated.",
            condition="pull_request_not_open",
            hint=hint,
        )
    if (
        expected_remote_target is None
        or discovered_pull_request.head.sha != expected_remote_target
    ):
        raise DriftError(
            t"Pull request #{review_identity.pr_number} and its remote branch no longer "
            t"identify the same commit.",
            condition="remote_branch_moved",
            hint=t"Inspect it with {ui.cmd('view')} before submitting again.",
        )


async def _sync_pull_request_metadata(
    *,
    github_client: GithubClient,
    labels: list[str],
    pull_request_number: int,
    reviewers: list[str],
    team_reviewers: list[str],
) -> None:
    try:
        if reviewers or team_reviewers:
            await github_client.request_reviewers(
                pull_number=pull_request_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if labels:
            await github_client.add_labels(
                issue_number=pull_request_number,
                labels=labels,
            )
    except GithubClientError as error:
        raise CliError(
            f"Could not synchronize metadata for pull request #{pull_request_number}"
        ) from error


def _submitted_identity(
    *,
    branch: str,
    github_client: GithubClient,
    pull_request: GithubPullRequest,
    review_identity: ReviewIdentity | None,
) -> ReviewIdentity:
    if review_identity is None:
        return ReviewIdentity(
            repository_owner=github_client.repository.owner,
            repository_name=github_client.repository.repo,
            pr_number=pull_request.number,
            head_owner=github_client.repository.owner,
            head_ref=branch,
        )
    return review_identity
