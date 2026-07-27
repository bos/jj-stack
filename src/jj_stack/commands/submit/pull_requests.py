"""Sync pull request state on GitHub for each prepared revision."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import jj_stack.ui as ui
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError, DriftError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubPullRequest, GithubPullRequestReview
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline

from .models import (
    PendingPullRequestSync,
    PullRequestAction,
    ResolvedSubmitOptions,
    SubmitMutationRun,
    SubmitOptions,
    SubmittedRevision,
)


async def discover_pull_requests_by_branch(
    *,
    github_client: GithubClient,
    branches: tuple[str, ...],
) -> dict[str, GithubPullRequest | None]:
    if not branches:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            head_refs=branches,
        )
    except GithubClientError as error:
        raise CliError("Could not batch pull request discovery for branches") from error

    return {
        branch: _select_discovered_pull_request(
            head_label=f"{github_client.repository.owner}:{branch}",
            pull_requests=discovered_pull_requests.get(branch, ()),
        )
        for branch in branches
    }


def ensure_pull_request_syncs_are_safe(
    *,
    options: SubmitOptions,
    pending_syncs: Sequence[PendingPullRequestSync],
    repository_key: tuple[str, str],
    state: ReviewState,
) -> None:
    """Verify every planned PR sync before any mutation.

    A damaged or divergent link anywhere in the plan must stop `submit` before
    review branches push or sibling PRs sync. Validating per change inside the
    concurrent sync phase would let a mid-stack link failure surface only after
    those mutations have already happened.
    """

    for pending_sync in pending_syncs:
        prepared_revision = pending_sync.prepared
        change_id = prepared_revision.revision.change_id
        review_identity = state.review_identities.get(change_id)
        submitted_baseline = state.submitted_baselines.get(change_id)
        _ensure_pull_request_link_is_consistent(
            branch=prepared_revision.branch,
            change_id=change_id,
            discovered_pull_request=pending_sync.discovered_pull_request,
            expected_remote_target=prepared_revision.expected_remote_target,
            repository_key=repository_key,
            review_identity=review_identity,
            submitted_baseline=submitted_baseline,
        )
        pull_request = pending_sync.discovered_pull_request
        if options.existing_only and (
            review_identity is None or submitted_baseline is None or pull_request is None
        ):
            raise CliError(
                t"Cannot sync {ui.change_id(change_id)} without its existing pull request.",
                hint=t"Repair the review link with {ui.cmd('relink')} before retrying.",
            )


async def sync_pull_requests(
    *,
    github_client: GithubClient,
    options: SubmitOptions,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    resolved_options: ResolvedSubmitOptions,
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
        items=pending_syncs,
        run_item=lambda pending_sync: _sync_pull_request(
            github_client=github_client,
            options=options,
            pending_sync=pending_sync,
            resolved_options=resolved_options,
            run=run,
        ),
        on_success=handle_success,
    )
    return tuple(submitted_revision for submitted_revision, _, _ in submitted_revisions)


async def _sync_pull_request(
    *,
    github_client: GithubClient,
    options: SubmitOptions,
    pending_sync: PendingPullRequestSync,
    resolved_options: ResolvedSubmitOptions,
    run: SubmitMutationRun,
) -> tuple[SubmittedRevision, ReviewIdentity | None, SubmittedBaseline | None]:
    prepared_revision = pending_sync.prepared
    branch = prepared_revision.branch
    change_id = prepared_revision.revision.change_id
    discovered_pull_request = pending_sync.discovered_pull_request
    review_identity = run.state.review_identities.get(change_id)

    title = pending_sync.generated_description.title
    body = pending_sync.generated_description.body
    if discovered_pull_request is None:
        if options.existing_only:
            raise AssertionError("Existing-only submit reached pull request creation.")
        pull_request = None
        if not run.dry_run:
            pull_request = await _create_pull_request(
                base_branch=pending_sync.base_branch,
                body=body,
                draft=(options.draft_mode in ("draft", "draft_all")),
                github_client=github_client,
                head_branch=branch,
                title=title,
            )
        action: PullRequestAction = "created"
    else:
        base_update = (
            pending_sync.base_branch
            if discovered_pull_request.base.ref != pending_sync.base_branch
            else None
        )
        body_update = body if (discovered_pull_request.body or "") != body else None
        title_update = title if discovered_pull_request.title != title else None
        pull_request = discovered_pull_request
        if base_update is None and body_update is None and title_update is None:
            action = "unchanged"
        else:
            if not run.dry_run:
                pull_request = await _update_pull_request(
                    base_branch=base_update,
                    body=body_update,
                    github_client=github_client,
                    pull_request=discovered_pull_request,
                    title=title_update,
                )
            action = "updated"

    if pull_request is not None and pull_request.state == "open":
        if options.draft_mode == "open" and pull_request.is_draft:
            if not run.dry_run:
                pull_request = await _mark_pull_request_ready_for_review(
                    github_client=github_client,
                    pull_request=pull_request,
                )
            action = "updated"
        elif options.draft_mode == "draft_all" and not pull_request.is_draft:
            if not run.dry_run:
                pull_request = await _convert_pull_request_to_draft(
                    github_client=github_client,
                    pull_request=pull_request,
                )
            action = "updated"

    if (
        not run.dry_run
        and pull_request is not None
        and (
            action != "unchanged"
            or options.reviewers is not None
            or options.team_reviewers is not None
        )
    ):
        await _sync_pull_request_metadata(
            github_client=github_client,
            labels=resolved_options.labels,
            pull_request_number=pull_request.number,
            reviewers=resolved_options.reviewers,
            team_reviewers=resolved_options.team_reviewers,
        )

    if not run.dry_run and options.re_request and pull_request is not None:
        re_request_reviewers = await _load_re_request_reviewers(
            github_client=github_client,
            pull_request_number=pull_request.number,
        )
        merged_reviewers = _merge_re_request_reviewers(
            reviewers=resolved_options.reviewers,
            re_request_reviewers=re_request_reviewers,
        )
        if merged_reviewers != resolved_options.reviewers:
            await _sync_pull_request_metadata(
                github_client=github_client,
                labels=[],
                pull_request_number=pull_request.number,
                reviewers=merged_reviewers,
                team_reviewers=[],
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
            pull_request_title=(pull_request.title if pull_request is not None else None),
            pull_request_url=(pull_request.html_url if pull_request is not None else None),
        ),
        next_identity,
        next_baseline,
    )


async def _load_re_request_reviewers(
    *,
    github_client: GithubClient,
    pull_request_number: int,
) -> list[str]:
    try:
        reviews = await github_client.list_pull_request_reviews(
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
        raise DriftError(
            t"GitHub reports multiple pull requests for head branch {ui.bookmark(head_label)}.",
            condition="pull_request_ambiguous",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if not pull_requests:
        return None
    pull_request = pull_requests[0]
    if pull_request.state != "open":
        raise DriftError(
            t"GitHub reports pull request #{pull_request.number} for head branch "
            t"{ui.bookmark(head_label)} in state {pull_request.state}.",
            condition="pull_request_not_open",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    return pull_request


def _ensure_pull_request_link_is_consistent(
    *,
    branch: str,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
    expected_remote_target: str | None,
    repository_key: tuple[str, str],
    review_identity: ReviewIdentity | None,
    submitted_baseline: SubmittedBaseline | None,
) -> None:
    if review_identity is None:
        if submitted_baseline is not None:
            raise CliError(
                t"Saved PR tracking for {ui.change_id(change_id)} has a last submitted "
                t"commit but no pull request number or branch.",
                hint=t"Repair it with {ui.cmd('relink')} before submitting again.",
            )
        if discovered_pull_request is not None:
            raise DriftError(
                t"GitHub already reports PR #{discovered_pull_request.number} for "
                t"untracked branch {ui.bookmark(branch)}.",
                condition="saved_pull_request_missing",
                hint=t"Adopt that PR explicitly with {ui.cmd('relink')} before submitting.",
            )
        return
    if submitted_baseline is None:
        raise CliError(
            t"Saved PR tracking for {ui.change_id(change_id)} has no last submitted commit.",
            hint=t"Repair it with {ui.cmd('relink')} before submitting again.",
        )
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


async def _create_pull_request(
    *,
    base_branch: str,
    body: str,
    draft: bool,
    github_client: GithubClient,
    head_branch: str,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.create_pull_request(
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


async def _mark_pull_request_ready_for_review(
    *,
    github_client: GithubClient,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not mark draft pull request #{pull_request.number} ready for review for "
            f"{github_client.repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.mark_pull_request_ready_for_review(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not mark draft pull request #{pull_request.number} ready for review for "
            f"{github_client.repository.full_name}"
        ) from error


async def _convert_pull_request_to_draft(
    *,
    github_client: GithubClient,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_client.repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.convert_pull_request_to_draft(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_client.repository.full_name}"
        ) from error


async def _update_pull_request(
    *,
    base_branch: str | None,
    body: str | None,
    github_client: GithubClient,
    pull_request: GithubPullRequest,
    title: str | None,
) -> GithubPullRequest:
    try:
        return await github_client.update_pull_request(
            pull_number=pull_request.number,
            base=base_branch,
            body=body,
            title=title,
        )
    except GithubClientError as error:
        raise CliError(f"Could not update pull request #{pull_request.number}") from error


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
