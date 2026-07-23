"""Pure landed evidence plus independent review finalization and retirement."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest
from jj_stack.review.bookmarks import bookmark_cleanup_allowed, classify_local_bookmark_forget
from jj_stack.ui import Message

from .landed_evidence import (
    LandedEvidenceKind,
    LandedReviewCandidate,
    classify_commit_ancestries,
    classify_exact_snapshot,
    collect_landed_evidence,
)
from .observation import RepositoryObservation, observe_review_mutation

LandedReviewOutcome = Literal["finalized", "already_terminal", "skipped"]


@dataclass(frozen=True, slots=True)
class LandedReviewResult:
    """Independent remote-finalization and local-retirement outcomes."""

    candidate: LandedReviewCandidate
    outcome: LandedReviewOutcome
    cleanup_warning: Message | None = None
    forgot_bookmark: bool = False
    retired_tracking: bool = False
    skip_reason: Message | None = None
    retirement_skip_reason: Message | None = None


@dataclass(frozen=True, slots=True)
class FinalizationContext:
    command: CommandContext
    dry_run: bool
    github: GithubClient
    remote_name: str
    trunk_branch: str
    trunk_commit_id: str


async def finalize_landed_reviews(
    *,
    candidates: tuple[LandedReviewCandidate, ...],
    finalizer: FinalizationContext,
    labels: dict[str, str] | None = None,
) -> tuple[LandedReviewResult, ...]:
    """Finalize only the supplied exact-snapshot candidates."""

    return tuple([
            await _finalize_landed_review(
                candidate=candidate,
                finalizer=finalizer,
                label=(labels or {}).get(candidate.change_id),
            )
            for candidate in candidates
    ])


async def _finalize_landed_review(
    *,
    candidate: LandedReviewCandidate,
    finalizer: FinalizationContext,
    label: str | None,
) -> LandedReviewResult:
    pull_request, reason = await _observe_exact_candidate(candidate, finalizer)
    if reason is not None or pull_request is None:
        return LandedReviewResult(candidate=candidate, outcome="skipped", skip_reason=reason)
    pull_request = pull_request.normalize_state()
    if pull_request.state != "open":
        return LandedReviewResult(candidate=candidate, outcome="already_terminal")
    if not finalizer.dry_run:
        rendered = label or candidate.change_id
        console.output(t"Finalizing PR #{pull_request.number} for {rendered}...")
        pull_request, reason = await _finalize_open_review(
            candidate=candidate,
            finalizer=finalizer,
            pull_request=pull_request,
        )
        if reason is not None or pull_request is None:
            return LandedReviewResult(candidate=candidate, outcome="skipped", skip_reason=reason)
    return LandedReviewResult(candidate=candidate, outcome="finalized")


async def _finalize_open_review(
    *,
    candidate: LandedReviewCandidate,
    finalizer: FinalizationContext,
    pull_request: GithubPullRequest,
) -> tuple[GithubPullRequest | None, Message | None]:
    try:
        if pull_request.base.ref != finalizer.trunk_branch:
            await finalizer.github.update_pull_request(
                pull_number=pull_request.number,
                base=finalizer.trunk_branch,
            )
            reloaded, reason = await _observe_exact_candidate(candidate, finalizer)
            if reason is not None or reloaded is None:
                return None, reason
            pull_request = reloaded.normalize_state()
            if pull_request.state != "open":
                return pull_request, None
            if pull_request.base.ref != finalizer.trunk_branch:
                return None, t"PR #{pull_request.number} did not stay retargeted to trunk"
        await finalizer.github.close_pull_request(pull_number=pull_request.number)
        close_conflict = False
    except GithubClientError as error:
        if error.status_code != 422:
            return None, t"could not finish cleanup for PR #{pull_request.number}: {error}"
        close_conflict = True
    reloaded, reason = await _observe_exact_candidate(candidate, finalizer)
    if reason is not None or reloaded is None:
        return None, reason
    reloaded = reloaded.normalize_state()
    if reloaded.state == "open":
        return None, t"GitHub still reports PR #{reloaded.number} open after closing it"
    if close_conflict and reloaded.state != "merged":
        return None, t"GitHub rejected the close for PR #{reloaded.number} without merging it"
    return reloaded, None


async def _observe_exact_candidate(
    candidate: LandedReviewCandidate,
    finalizer: FinalizationContext,
) -> tuple[GithubPullRequest | None, Message | None]:
    observation, reason = await observe_landed_candidate(candidate, finalizer)
    if reason is not None or observation is None:
        return None, reason
    pull_request = observation.reviews[candidate.change_id].pull_request
    if pull_request is None:
        return None, t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
    ancestry = classify_commit_ancestries(
        commit_ids=(candidate.submitted_baseline.commit_id,),
        context=finalizer.command,
        trunk_commit_id=finalizer.trunk_commit_id,
    )[candidate.submitted_baseline.commit_id]
    evidence = classify_exact_snapshot(
        ancestry=ancestry,
        candidate=candidate,
        pull_request=pull_request,
        repository=finalizer.github.repository,
    )
    if evidence.state != "landed":
        return None, evidence.reason or "the submitted commit is not confirmed on trunk"
    review = observation.reviews[candidate.change_id]
    if pull_request.normalize_state().state == "open" and (
        len(review.head_pull_requests) != 1
        or review.head_pull_requests[0].number != pull_request.number
    ):
        return None, "the review branch no longer identifies exactly the saved pull request"
    return pull_request, None


async def observe_landed_candidate(
    candidate: LandedReviewCandidate,
    finalizer: FinalizationContext,
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
        return None, t"could not recheck the repository and pull request: {error}"
    if observation.remote is None or observation.remote.name != finalizer.remote_name:
        return None, t"Git remote {ui.bookmark(finalizer.remote_name)} is no longer configured"
    if observation.configured_repository != finalizer.github.repository:
        return None, "the configured remote no longer names the expected GitHub repository"
    github_repository = observation.github_repository
    if github_repository.full_name.casefold() != finalizer.github.repository.full_name.casefold():
        return None, "GitHub no longer reports the expected repository"
    if github_repository.default_branch not in (None, "", finalizer.trunk_branch):
        return None, "GitHub no longer reports the expected trunk branch as its default"
    fetched_trunk = observation.fetched_trunk
    if fetched_trunk is None or fetched_trunk.commit_id != finalizer.trunk_commit_id:
        return None, t"fetched {ui.revset('trunk()')} changed while checking the landed PR"
    if observation.remote_trunk_target != finalizer.trunk_commit_id:
        return None, "the live trunk ref moved after the last fetch"
    review = observation.reviews[candidate.change_id]
    if (
        review.identity != candidate.review_identity
        or review.baseline != candidate.submitted_baseline
        or candidate.change_id in observation.duplicate_claim_change_ids
    ):
        return None, "saved PR tracking changed while checking the landed PR"
    return observation, None


async def retire_landed_reviews(
    *,
    cleanup_bookmarks: bool,
    evidence: dict[str, LandedEvidenceKind],
    finalization_results: tuple[LandedReviewResult, ...],
    finalizer: FinalizationContext,
    retirement_blocker: Callable[[LandedReviewCandidate], Message | None] | None = None,
) -> tuple[LandedReviewResult, ...]:
    """Retire links independently after remote finalization has reached a terminal state."""

    context = finalizer.command

    async def current_retirement_error(
        candidate: LandedReviewCandidate,
    ) -> Message | None:
        blocker = retirement_blocker(candidate) if retirement_blocker is not None else None
        return blocker or await _retirement_authority_error(
            candidate=candidate,
            evidence_kind=evidence[candidate.change_id],
            finalizer=finalizer,
        )

    results = list(finalization_results)
    active_results = filter(lambda item: item[1].outcome != "skipped", enumerate(results))
    for index, result in active_results:
        candidate = result.candidate
        reason = await current_retirement_error(candidate)
        if reason is not None:
            results[index] = replace(result, retirement_skip_reason=reason)
            continue
        forgot = False
        cleanup_warning: Message | None = None
        bookmark = candidate.review_identity.head_ref
        if cleanup_bookmarks and bookmark_cleanup_allowed(
            bookmark=bookmark,
            bookmark_managed=candidate.review_identity.manages_bookmark,
            cleanup_user_bookmarks=context.config.cleanup_user_bookmarks,
            prefix=context.config.bookmark_prefix,
        ):
            bookmark_state = context.jj_client.get_bookmark_state(bookmark)
            forgot = (
                classify_local_bookmark_forget(
                    bookmark_state=bookmark_state,
                    expected_commit_id=candidate.submitted_baseline.commit_id,
                )
                == "safe"
            )
            if forgot and not finalizer.dry_run:
                try:
                    context.jj_client.forget_bookmarks((bookmark,))
                except JjCommandError as error:
                    forgot = False
                    cleanup_warning = t"bookmark cleanup failed: {error}"
        if not finalizer.dry_run:
            reason = await current_retirement_error(candidate)
            if reason is not None:
                results[index] = replace(
                    result,
                    cleanup_warning=cleanup_warning,
                    forgot_bookmark=forgot,
                    retirement_skip_reason=reason,
                )
                continue
            try:
                context.state_store.retire_review(
                    candidate.change_id,
                    expected_identity=candidate.review_identity,
                    expected_baseline=candidate.submitted_baseline,
                )
            except (OSError, RuntimeError, ValueError) as error:
                results[index] = replace(
                    result,
                    cleanup_warning=cleanup_warning,
                    forgot_bookmark=forgot,
                    retirement_skip_reason=str(error),
                )
                continue
        results[index] = replace(
            result,
            cleanup_warning=cleanup_warning,
            forgot_bookmark=forgot,
            retired_tracking=True,
        )
    return tuple(results)


async def _retirement_authority_error(
    *,
    candidate: LandedReviewCandidate,
    evidence_kind: LandedEvidenceKind,
    finalizer: FinalizationContext,
) -> Message | None:
    local_revisions = finalizer.command.jj_client.query_revisions_by_change_ids(
        (candidate.change_id,)
    ).get(candidate.change_id, ())
    if any(
        revision.commit_id != candidate.submitted_baseline.commit_id
        for revision in local_revisions
        if not revision.immutable
    ):
        return t"{ui.change_id(candidate.change_id)} has unpublished local edits"
    observation, reason = await observe_landed_candidate(candidate, finalizer)
    if reason is not None or observation is None:
        return reason
    pull_request = observation.reviews[candidate.change_id].pull_request
    if pull_request is None:
        return t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
    exact, rewritten = collect_landed_evidence(
        candidate=candidate,
        context=finalizer.command,
        pull_request=pull_request,
        repository=finalizer.github.repository,
        trunk_commit_id=finalizer.trunk_commit_id,
    )
    if evidence_kind == "exact":
        return None if exact.state == "landed" else exact.reason or exact.state
    return None if rewritten.state == "landed" else rewritten.reason or rewritten.state
