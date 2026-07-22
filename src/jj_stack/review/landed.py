"""Observational finalization for exact submitted commits already on fetched trunk."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import GithubRepoAddress, resolve_trunk_branch
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.bookmarks import (
    bookmark_cleanup_allowed,
    classify_local_bookmark_forget,
)
from jj_stack.ui import Message

from .observation import RepositoryObservation, observe_review_mutation

LandedReviewOutcome = Literal["finalized", "already_terminal", "skipped"]


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


def _skipped(candidate: LandedReviewCandidate, reason: Message | None) -> LandedReviewResult:
    return LandedReviewResult(candidate=candidate, outcome="skipped", skip_reason=reason)


@dataclass(frozen=True, slots=True)
class _FinalizationContext:
    cleanup_bookmarks: bool
    command: CommandContext
    dry_run: bool
    github: GithubClient
    remote_name: str
    trunk_branch: str
    trunk_commit_id: str


def landed_review_candidates(
    *,
    jj_client: JjClient,
    state: ReviewState,
    trunk_commit_id: str,
) -> tuple[LandedReviewCandidate, ...]:
    """Return tracked reviews whose exact submitted commit is an ancestor of trunk."""

    saved = tuple(
        LandedReviewCandidate(
            change_id=change_id,
            review_identity=review_identity,
            submitted_baseline=submitted_baseline,
        )
        for change_id, review_identity in sorted(state.review_identities.items())
        if review_identity.is_tracked
        if (submitted_baseline := state.submitted_baselines.get(change_id)) is not None
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
    cleanup_bookmarks: bool,
    context: CommandContext,
    dry_run: bool = False,
    github_client: GithubClient,
    labels: dict[str, str] | None = None,
    order: tuple[str, ...] = (),
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> tuple[LandedReviewResult, ...]:
    """Finalize exact-on-trunk reviews in requested order, then by change ID."""

    state = context.state_store.load()
    candidates = landed_review_candidates(
        jj_client=context.jj_client,
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
    finalizer = _FinalizationContext(
        cleanup_bookmarks=cleanup_bookmarks,
        command=context,
        dry_run=dry_run,
        github=github_client,
        remote_name=remote_name,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )
    results: list[LandedReviewResult] = []
    for candidate in candidates:
        result = await _finalize_landed_review(
            candidate=candidate,
            finalizer=finalizer,
            label=(labels or {}).get(candidate.change_id),
        )
        if result.outcome != "skipped" and not dry_run:
            _, skip_reason = await _observe_landed_candidate(candidate, finalizer)
            if skip_reason is not None:
                result = _skipped(candidate, t"tracking retirement skipped: {skip_reason}")
            else:
                context.state_store.retire_review(
                    candidate.change_id,
                    expected_identity=candidate.review_identity,
                    expected_baseline=candidate.submitted_baseline,
                )
        results.append(result)
    return tuple(results)


async def _finalize_landed_review(
    *,
    candidate: LandedReviewCandidate,
    finalizer: _FinalizationContext,
    label: str | None = None,
) -> LandedReviewResult:
    observation, skip_reason = await _observe_landed_candidate(candidate, finalizer)
    if skip_reason is not None or observation is None:
        return _skipped(candidate, skip_reason)
    if not finalizer.dry_run:
        rendered_label = (
            t"{label} {ui.change_id(candidate.change_id)}"
            if label is not None
            else t"{ui.change_id(candidate.change_id)}"
        )
        console.output(
            t"Finalizing PR #{candidate.review_identity.pr_number} for {rendered_label}..."
        )
    observed_review = observation.reviews[candidate.change_id]
    pull_request = observed_review.pull_request
    if pull_request is None:
        raise AssertionError("Fresh landed observation requires a pull request.")
    pull_request = pull_request.normalize_state()

    outcome: LandedReviewOutcome = "already_terminal"
    if pull_request.state == "open":
        outcome = "finalized"
        if not finalizer.dry_run:
            pull_request, skip_reason = await _finalize_open_review(
                candidate=candidate,
                finalizer=finalizer,
                pull_request=pull_request,
            )
            if skip_reason is not None or pull_request is None:
                return _skipped(candidate, skip_reason)

    bookmark_state = finalizer.command.jj_client.get_bookmark_state(
        candidate.review_identity.head_ref
    )
    forget_bookmark = (
        finalizer.cleanup_bookmarks
        and bookmark_cleanup_allowed(
            bookmark=candidate.review_identity.head_ref,
            bookmark_managed=candidate.review_identity.manages_bookmark,
            cleanup_user_bookmarks=finalizer.command.config.cleanup_user_bookmarks,
            prefix=finalizer.command.config.bookmark_prefix,
        )
        and classify_local_bookmark_forget(
            bookmark_state=bookmark_state,
            expected_commit_id=candidate.submitted_baseline.commit_id,
        )
        == "safe"
    )
    if not finalizer.dry_run:
        if forget_bookmark:
            finalizer.command.jj_client.forget_bookmarks((candidate.review_identity.head_ref,))
    return LandedReviewResult(
        candidate=candidate,
        outcome=outcome,
        forgot_bookmark=forget_bookmark,
    )


async def _finalize_open_review(
    *,
    candidate: LandedReviewCandidate,
    finalizer: _FinalizationContext,
    pull_request: GithubPullRequest,
) -> tuple[GithubPullRequest | None, Message | None]:
    try:
        if pull_request.base.ref != finalizer.trunk_branch:
            await finalizer.github.update_pull_request(
                pull_number=pull_request.number,
                base=finalizer.trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
            reloaded, reason = await _reload_landed_pull_request(
                candidate=candidate,
                finalizer=finalizer,
            )
            if reason is not None or reloaded is None:
                return None, reason
            pull_request = reloaded
            if pull_request.state != "open":
                return pull_request, None
            if pull_request.base.ref != finalizer.trunk_branch:
                return (
                    None,
                    t"GitHub still reports PR #{pull_request.number} based on "
                    t"{ui.bookmark(pull_request.base.ref)} after retargeting",
                )
        await finalizer.github.close_pull_request(pull_number=pull_request.number)
        close_conflict = False
    except GithubClientError as error:
        if error.status_code != 422:
            return None, t"could not finalize PR #{pull_request.number}: {error}"
        close_conflict = True
    reloaded, reason = await _reload_landed_pull_request(
        candidate=candidate,
        finalizer=finalizer,
    )
    if reason is not None or reloaded is None:
        return None, reason
    pull_request = reloaded
    if pull_request.state == "open":
        return None, t"GitHub still reports PR #{pull_request.number} open after closing it"
    if close_conflict and pull_request.state != "merged":
        return None, t"GitHub rejected the close for PR #{pull_request.number} without merging it"
    return pull_request, None


async def _reload_landed_pull_request(
    *,
    candidate: LandedReviewCandidate,
    finalizer: _FinalizationContext,
) -> tuple[GithubPullRequest | None, Message | None]:
    observation, reason = await _observe_landed_candidate(candidate, finalizer)
    if reason is not None or observation is None:
        return None, reason
    pull_request = observation.reviews[candidate.change_id].pull_request
    if pull_request is None:
        raise AssertionError("Fresh landed observation requires a pull request.")
    return pull_request.normalize_state(), None


async def _observe_landed_candidate(
    candidate: LandedReviewCandidate,
    finalizer: _FinalizationContext,
) -> tuple[RepositoryObservation | None, Message | None]:
    try:
        observation = await observe_review_mutation(
            change_ids=(candidate.change_id,),
            context=finalizer.command,
            github_client=finalizer.github,
            remote_name=finalizer.remote_name,
            trunk_branch=finalizer.trunk_branch,
        )
    except (CliError, GithubClientError, JjCommandError) as error:
        return None, t"could not reload current review authority: {error}"
    reason = _landed_observation_mismatch(candidate, finalizer, observation)
    return (None, reason) if reason is not None else (observation, None)


def _landed_observation_mismatch(
    candidate: LandedReviewCandidate,
    finalizer: _FinalizationContext,
    observation: RepositoryObservation,
) -> Message | None:
    error = _landed_repository_mismatch(candidate, finalizer, observation)
    if error is not None:
        return error
    return _landed_review_mismatch(
        candidate=candidate,
        github_client=finalizer.github,
        observation=observation,
    )


def _landed_repository_mismatch(
    candidate: LandedReviewCandidate,
    finalizer: _FinalizationContext,
    observation: RepositoryObservation,
) -> Message | None:
    if observation.remote is None or observation.remote.name != finalizer.remote_name:
        return t"Git remote {ui.bookmark(finalizer.remote_name)} is no longer configured"
    if observation.configured_repository != finalizer.github.repository:
        return "the configured remote no longer names the expected GitHub repository"
    identity = candidate.review_identity
    expected_repository = finalizer.github.repository
    if (
        identity.github_host != expected_repository.host
        or identity.repository_owner.casefold() != expected_repository.owner.casefold()
        or identity.repository_name.casefold() != expected_repository.repo.casefold()
    ):
        return "saved review identity no longer names the expected GitHub repository"
    if (
        observation.github_repository.full_name.casefold()
        != finalizer.github.repository.full_name.casefold()
    ):
        return "GitHub no longer reports the expected repository"
    if observation.github_repository.default_branch not in (None, "", finalizer.trunk_branch):
        return "GitHub no longer reports the expected trunk branch as its default"
    fetched_trunk = observation.fetched_trunk
    if fetched_trunk is None or fetched_trunk.commit_id != finalizer.trunk_commit_id:
        return t"fetched {ui.revset('trunk()')} changed during finalization"
    if observation.remote_trunk_target != fetched_trunk.commit_id:
        return "the live trunk ref moved after the last fetch"
    landed = finalizer.command.jj_client.query_commit_ids_ancestors_of(
        (candidate.submitted_baseline.commit_id,),
        descendant_commit_id=fetched_trunk.commit_id,
    )
    if candidate.submitted_baseline.commit_id not in landed:
        return "the exact submitted commit is no longer on fetched trunk"
    return None


def _landed_review_mismatch(
    *,
    candidate: LandedReviewCandidate,
    github_client: GithubClient,
    observation: RepositoryObservation,
) -> Message | None:
    review = observation.reviews[candidate.change_id]
    if (
        review.identity != candidate.review_identity
        or candidate.change_id in observation.duplicate_claim_change_ids
    ):
        return "saved review identity changed during finalization"
    if review.baseline != candidate.submitted_baseline:
        return "submitted baseline changed during finalization"
    if (
        review.local_revision is None
        or review.local_revision.commit_id != candidate.submitted_baseline.commit_id
    ):
        return t"{ui.change_id(candidate.change_id)} has local edits since its last submit"
    if review.remote_review_target != candidate.submitted_baseline.commit_id:
        return "the live review ref no longer matches the exact submitted commit"
    pull_request = review.pull_request
    if pull_request is None:
        return t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
    mismatch = landed_pull_request_head_mismatch(
        bookmark=candidate.review_identity.head_ref,
        commit_id=candidate.submitted_baseline.commit_id,
        github_client=github_client,
        head_owner=candidate.review_identity.head_owner,
        pull_request=pull_request,
    )
    if mismatch is not None:
        return mismatch
    if pull_request.normalize_state().state == "open" and (
        len(review.head_pull_requests) != 1
        or review.head_pull_requests[0].number != pull_request.number
    ):
        return "the live review ref no longer identifies exactly the saved pull request"
    return None


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


def run_landed_review_sweep(
    *,
    cleanup_bookmarks: bool,
    context: CommandContext,
    dry_run: bool = False,
    remote_name: str,
    repository: GithubRepoAddress,
    trunk_commit_id: str,
) -> tuple[LandedReviewResult, ...]:
    """Build a GitHub client and run observational finalization synchronously."""

    async def _run() -> tuple[LandedReviewResult, ...]:
        async with build_github_client(repository=repository) as github_client:
            try:
                repository_state = await github_client.get_repository()
            except GithubClientError:
                return ()
            trunk_branch = resolve_trunk_branch(
                bookmark_states=context.jj_client.list_bookmark_states(),
                github_repository_state=repository_state,
                remote_name=remote_name,
                trunk_commit_id=trunk_commit_id,
            )
            return await finalize_landed_reviews(
                cleanup_bookmarks=cleanup_bookmarks,
                context=context,
                dry_run=dry_run,
                github_client=github_client,
                remote_name=remote_name,
                trunk_branch=trunk_branch,
                trunk_commit_id=trunk_commit_id,
            )

    return asyncio.run(_run())
