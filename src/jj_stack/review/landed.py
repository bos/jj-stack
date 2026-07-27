from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, error_message
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest
from jj_stack.ui import Message

from .landed_evidence import (
    LandedEvidenceKind,
    LandedReviewCandidate,
    classify_commit_ancestries,
    classify_exact_snapshot,
    collect_landed_evidence,
    holds_unpublished_edit,
)
from .observation import RepositoryObservation, observe_reviews

LandedReviewOutcome = Literal["finalized", "already_terminal", "skipped"]


@dataclass(frozen=True, slots=True)
class LandedReviewResult:
    candidate: LandedReviewCandidate
    outcome: LandedReviewOutcome
    retired_tracking: bool = False
    skip_reason: Message | None = None
    retirement_skip_reason: Message | None = None
    retirement_failure: Message | None = None


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

    return tuple(
        [
            await _finalize_review(candidate, finalizer, (labels or {}).get(candidate.change_id))
            for candidate in candidates
        ]
    )


async def _finalize_review(
    candidate: LandedReviewCandidate,
    finalizer: FinalizationContext,
    label: str | None,
) -> LandedReviewResult:
    pull_request, reason = await _observe_exact_candidate(candidate, finalizer)
    if reason is not None or pull_request is None:
        return LandedReviewResult(candidate=candidate, outcome="skipped", skip_reason=reason)
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
            pull_request = reloaded
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
    pull_request = pull_request.normalize_state()
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
    if pull_request.state == "open" and (
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
        observation = await observe_reviews(
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
    assert github_repository is not None
    if github_repository.full_name.casefold() != finalizer.github.repository.full_name.casefold():
        return None, "GitHub no longer reports the expected repository"
    if github_repository.default_branch not in (None, "", finalizer.trunk_branch):
        return None, "GitHub no longer reports the expected trunk branch as its default"
    if observation.fetched_trunk_commit_id != finalizer.trunk_commit_id:
        return None, t"fetched {ui.revset('trunk()')} changed while checking the merged PR"
    if observation.remote_trunk_target != finalizer.trunk_commit_id:
        return None, "the live trunk ref moved after the last fetch"
    review = observation.reviews[candidate.change_id]
    if (
        review.identity != candidate.review_identity
        or review.baseline != candidate.submitted_baseline
        or candidate.change_id in observation.duplicate_claim_change_ids
    ):
        return None, "saved PR tracking changed while checking the merged PR"
    return observation, None


async def retire_landed_reviews(
    *,
    evidence: dict[str, LandedEvidenceKind],
    finalization_results: tuple[LandedReviewResult, ...],
    finalizer: FinalizationContext,
    retirement_blocker: Callable[[LandedReviewCandidate], Message | None] | None = None,
    terminal_required: frozenset[str] = frozenset(),
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
            terminal_required=candidate.change_id in terminal_required,
        )

    results = list(finalization_results)
    active_results = filter(lambda item: item[1].outcome != "skipped", enumerate(results))
    for index, result in active_results:
        candidate = result.candidate
        reason = await current_retirement_error(candidate)
        if reason is not None:
            results[index] = replace(result, retirement_skip_reason=reason)
            continue
        if not finalizer.dry_run:
            try:
                context.state_store.retire_review(
                    candidate.change_id,
                    expected_identity=candidate.review_identity,
                    expected_baseline=candidate.submitted_baseline,
                )
            except (OSError, RuntimeError, ValueError) as error:
                results[index] = replace(result, retirement_failure=error_message(error))
                continue
        results[index] = replace(
            result,
            retired_tracking=True,
        )
    return tuple(results)


async def _retirement_authority_error(
    *,
    candidate: LandedReviewCandidate,
    evidence_kind: LandedEvidenceKind,
    finalizer: FinalizationContext,
    terminal_required: bool,
) -> Message | None:
    local_revisions = finalizer.command.jj_client.query_revisions_by_change_ids(
        (candidate.change_id,)
    ).get(candidate.change_id, ())
    if any(
        holds_unpublished_edit(
            published_commit_ids=(candidate.submitted_baseline.commit_id,),
            revision=revision,
        )
        for revision in local_revisions
    ):
        return t"{ui.change_id(candidate.change_id)} has unpublished local edits"
    observation, reason = await observe_landed_candidate(candidate, finalizer)
    if reason is not None or observation is None:
        return reason
    pull_request = observation.reviews[candidate.change_id].pull_request
    if pull_request is None:
        return t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
    if terminal_required and pull_request.normalize_state().state != "merged":
        return t"native member PR #{pull_request.number} is not terminally merged"
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


def landed_exit_code(*, base: int, results: Sequence[LandedReviewResult]) -> int:
    """Fold a failed tracking removal into a command's exit status.

    Preserving tracking a dependent stack still needs is ordinary operation and leaves the
    base status alone; a durable write that was authorized and then failed is a failure a
    scripted caller has to be able to see.
    """

    return 1 if any(result.retirement_failure is not None for result in results) else base


def render_landed_results(
    *,
    dry_run: bool,
    results: tuple[LandedReviewResult, ...],
) -> None:
    """Render independent finalization and retirement outcomes."""

    if not results:
        return
    console.output(
        "Planned cleanup for merged PRs:" if dry_run else "Applied cleanup for merged PRs:"
    )
    marker = "•" if dry_run else "✓"
    for result in results:
        candidate = result.candidate
        if result.outcome == "skipped":
            console.output(
                t"  ! leave {ui.change_id(candidate.change_id)} unchanged: {result.skip_reason}"
            )
            continue
        if result.outcome == "finalized":
            console.output(t"  {marker} finish merged PR #{candidate.review_identity.pr_number}")
        if result.retired_tracking:
            console.output(t"  {marker} remove tracking for {ui.change_id(candidate.change_id)}")
        elif result.retirement_failure is not None:
            console.output(
                t"  ✗ could not remove tracking for {ui.change_id(candidate.change_id)}: "
                t"{result.retirement_failure}. Resolve that, then run "
                t"{ui.cmd('jj-stack cleanup')}."
            )
        elif result.retirement_skip_reason is not None:
            console.output(
                t"  ! leave {ui.change_id(candidate.change_id)} tracked: "
                t"{result.retirement_skip_reason}"
            )
