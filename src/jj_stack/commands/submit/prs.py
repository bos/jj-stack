"""Sync pull request state on GitHub for each prepared change."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

import jj_stack.ui as ui
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubPR, GithubPRReview
from jj_stack.models.tracking import (
    PRIdentity,
    SubmittedBaseline,
)
from jj_stack.state.store import TrackingStore
from jj_stack.ui import Message

from .models import (
    PRDraftAction,
    PRSyncPlan,
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


async def load_re_request_reviewers(
    *,
    github_client: GithubClient,
    prs: tuple[GithubPR, ...],
) -> dict[int, list[str]]:
    reviews = await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=prs,
        run_item=lambda pr: _github_request(
            github_client.list_pr_reviews(pr_number=pr.number),
            error_message=t"Could not load reviews for pull request "
            t"{format_pr_number(pr.number, url=pr.html_url)}",
        ),
    )
    return {
        pr.number: _reviewers_to_re_request(pr_reviews)
        for pr, pr_reviews in zip(prs, reviews, strict=True)
    }


async def sync_prs(
    *,
    github_client: GithubClient,
    plans: tuple[PRSyncPlan, ...],
    state_store: TrackingStore,
    on_progress: Callable[[], None],
) -> tuple[tuple[PRSyncPlan, GithubPR], ...]:
    submitted_changes = await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=plans,
        run_item=lambda plan: _sync_pr(
            github_client=github_client,
            plan=plan,
            state_store=state_store,
        ),
        on_success=on_progress,
    )
    return tuple(submitted_changes)


async def _sync_pr(
    *,
    github_client: GithubClient,
    plan: PRSyncPlan,
    state_store: TrackingStore,
) -> tuple[PRSyncPlan, GithubPR]:
    prepared_change = plan.prepared
    branch = prepared_change.branch
    change_id = prepared_change.change.change_id
    pr = plan.prepared.pr
    base_update, body_update, title_update = plan.content_updates

    if pr is None:
        pr = await _github_request(
            github_client.create_pr(
                base=plan.base_branch,
                body=plan.generated_description.body,
                draft=plan.draft,
                head=branch,
                title=plan.generated_description.title,
            ),
            error_message=t"Could not create a pull request for branch {ui.bookmark(branch)}",
        )
    elif any(update is not None for update in (base_update, body_update, title_update)):
        pr_number = format_pr_number(pr.number, url=pr.html_url)
        pr = await _github_request(
            github_client.update_pr(
                pr_number=pr.number,
                base=base_update,
                body=body_update,
                title=title_update,
            ),
            error_message=t"Could not update pull request {pr_number}",
        )

    # Save the PR link as soon as GitHub acknowledges the pull request and the pushed
    # branch. Draft state, labels and reviewers can be observed and rewritten on a
    # rerun, but a pull request submit created and never recorded leaves the change
    # untracked, and every retry then demands an explicit relink.
    baseline = SubmittedBaseline(commit_id=prepared_change.change.commit_id)
    identity = PRIdentity(pr_number=pr.number, head_ref=branch)
    state_store.relink_pr(change_id, identity=identity, baseline=baseline)
    pr = await _apply_draft_action(
        action=plan.draft_action,
        github_client=github_client,
        pr=pr,
    )
    if plan.metadata is not None:
        await _sync_pr_metadata(
            github_client=github_client,
            labels=plan.metadata.labels,
            pr_number=pr.number,
            reviewers=plan.metadata.reviewers,
            team_reviewers=plan.metadata.team_reviewers,
        )

    return plan, pr


async def _apply_draft_action(
    *,
    action: PRDraftAction | None,
    github_client: GithubClient,
    pr: GithubPR,
) -> GithubPR:
    if action is None:
        return pr
    pr_number = format_pr_number(pr.number, url=pr.html_url)
    message = (
        t"Could not return pull request {pr_number} to draft for {github_client.repo.full_name}"
        if action == "draft"
        else t"Could not mark draft pull request {pr_number} ready for review for "
        t"{github_client.repo.full_name}"
    )
    request = (
        github_client.convert_pr_to_draft(pr_id=pr.node_id)
        if action == "draft"
        else github_client.mark_pr_ready_for_review(pr_id=pr.node_id)
    )
    return await _github_request(request, error_message=message)


def _reviewers_to_re_request(
    reviews: Sequence[GithubPRReview],
) -> list[str]:
    latest_reviews_by_user: dict[str, GithubPRReview] = {}
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


async def _sync_pr_metadata(
    *,
    github_client: GithubClient,
    labels: list[str],
    pr_number: int,
    reviewers: list[str],
    team_reviewers: list[str],
) -> None:
    try:
        if reviewers or team_reviewers:
            await github_client.request_reviewers(
                pr_number=pr_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if labels:
            await github_client.add_labels(
                issue_number=pr_number,
                labels=labels,
            )
    except GithubClientError as error:
        pr_label = format_pr_number(pr_number, repo=github_client.repo)
        raise CliError(
            t"Could not update the reviewers or labels of pull request {pr_label}"
        ) from error
