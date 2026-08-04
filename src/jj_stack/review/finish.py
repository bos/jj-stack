"""Finish reviews whose work is proven to be on trunk, then stop tracking them.

Finishing and untracking are kept separate: a pull request GitHub has ended is finished for good,
while its tracking has to survive until nothing else needs it. Each step rechecks the evidence
against live GitHub and trunk immediately before it acts.
"""

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

from .observation import RepositoryObservation, observe_reviews
from .trunk_evidence import (
    TrackedReview,
    TrunkEvidenceKind,
    classify_commit_ancestries,
    classify_exact_snapshot,
    collect_trunk_evidence,
)

ReviewFinishOutcome = Literal["finished", "already_terminal", "skipped"]


@dataclass(frozen=True, slots=True)
class ReviewFinishResult:
    candidate: TrackedReview
    outcome: ReviewFinishOutcome
    retired_tracking: bool = False
    skip_reason: Message | None = None
    retirement_skip_reason: Message | None = None
    retirement_failure: Message | None = None


@dataclass(frozen=True, slots=True)
class FinishContext:
    command: CommandContext
    dry_run: bool
    github: GithubClient
    remote_name: str
    trunk_branch: str
    trunk_commit_id: str


async def finish_reviews(
    *,
    candidates: tuple[TrackedReview, ...],
    finish: FinishContext,
    skip_finish: frozenset[str] = frozenset(),
) -> tuple[ReviewFinishResult, ...]:
    """Finish the supplied reviews, reporting skipped ones as already terminal.

    Callers pass their whole candidate list and name the ones GitHub has already finished, so
    they do not each have to split the list and reinterleave the results afterwards.
    """

    return tuple(
        [
            ReviewFinishResult(candidate=candidate, outcome="already_terminal")
            if candidate.change_id in skip_finish
            else await _finish_review(candidate, finish)
            for candidate in candidates
        ]
    )


async def _finish_review(
    candidate: TrackedReview,
    finish: FinishContext,
) -> ReviewFinishResult:
    pull_request, reason = await _observe_review_on_trunk(candidate, finish)
    if reason is not None or pull_request is None:
        return ReviewFinishResult(candidate=candidate, outcome="skipped", skip_reason=reason)
    if pull_request.state != "open":
        return ReviewFinishResult(candidate=candidate, outcome="already_terminal")
    if not finish.dry_run:
        console.output(t"Finishing PR #{pull_request.number} for {candidate.change_id}...")
        pull_request, reason = await _finish_open_review(
            candidate=candidate,
            finish=finish,
            pull_request=pull_request,
        )
        if reason is not None or pull_request is None:
            return ReviewFinishResult(candidate=candidate, outcome="skipped", skip_reason=reason)
    return ReviewFinishResult(candidate=candidate, outcome="finished")


async def _finish_open_review(
    *,
    candidate: TrackedReview,
    finish: FinishContext,
    pull_request: GithubPullRequest,
) -> tuple[GithubPullRequest | None, Message | None]:
    try:
        if pull_request.base.ref != finish.trunk_branch:
            await finish.github.update_pull_request(
                pull_number=pull_request.number,
                base=finish.trunk_branch,
            )
            reloaded, reason = await _observe_review_on_trunk(candidate, finish)
            if reason is not None or reloaded is None:
                return None, reason
            pull_request = reloaded
            if pull_request.state != "open":
                return pull_request, None
            if pull_request.base.ref != finish.trunk_branch:
                return None, t"PR #{pull_request.number} did not stay retargeted to trunk"
        await finish.github.close_pull_request(pull_number=pull_request.number)
        close_conflict = False
    except GithubClientError as error:
        if error.status_code != 422:
            return None, t"could not finish cleanup for PR #{pull_request.number}: {error}"
        close_conflict = True
    reloaded, reason = await _observe_review_on_trunk(candidate, finish)
    if reason is not None or reloaded is None:
        return None, reason
    if reloaded.state == "open":
        return None, t"GitHub still reports PR #{reloaded.number} open after closing it"
    if close_conflict and reloaded.state != "merged":
        return None, t"GitHub rejected the close for PR #{reloaded.number} without merging it"
    return reloaded, None


async def _observe_review_on_trunk(
    candidate: TrackedReview,
    finish: FinishContext,
) -> tuple[GithubPullRequest | None, Message | None]:
    observation, reason = await observe_tracked_review(candidate, finish)
    if reason is not None or observation is None:
        return None, reason
    pull_request = observation.reviews[candidate.change_id].pull_request
    if pull_request is None:
        return None, t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
    pull_request = pull_request.normalize_state()
    ancestry = classify_commit_ancestries(
        commit_ids=(candidate.submitted_baseline.commit_id,),
        context=finish.command,
        trunk_commit_id=finish.trunk_commit_id,
    )[candidate.submitted_baseline.commit_id]
    evidence = classify_exact_snapshot(
        ancestry=ancestry,
        candidate=candidate,
        pull_request=pull_request,
        repository=finish.github.repository,
    )
    if not evidence.on_trunk:
        return None, evidence.reason or "the submitted commit is not confirmed on trunk"
    review = observation.reviews[candidate.change_id]
    if pull_request.state == "open" and (
        len(review.head_pull_requests) != 1
        or review.head_pull_requests[0].number != pull_request.number
    ):
        return None, "the review branch no longer identifies exactly the saved pull request"
    return pull_request, None


async def observe_tracked_review(
    candidate: TrackedReview,
    finish: FinishContext,
) -> tuple[RepositoryObservation | None, Message | None]:
    try:
        observation = await observe_reviews(
            change_ids=(candidate.change_id,),
            context=finish.command,
            github_client=finish.github,
            remote_name=finish.remote_name,
            trunk_branch=finish.trunk_branch,
        )
    except (CliError, GithubClientError, JjCommandError) as error:
        return None, t"could not recheck the repository and pull request: {error}"
    if observation.remote is None or observation.remote.name != finish.remote_name:
        return None, t"Git remote {ui.bookmark(finish.remote_name)} is no longer configured"
    if observation.configured_repository != finish.github.repository:
        return None, "the configured remote no longer names the expected GitHub repository"
    github_repository = observation.github_repository
    assert github_repository is not None
    if github_repository.full_name.casefold() != finish.github.repository.full_name.casefold():
        return None, "GitHub no longer reports the expected repository"
    if github_repository.default_branch not in (None, "", finish.trunk_branch):
        return None, "GitHub no longer reports the expected trunk branch as its default"
    # Evidence here means "an ancestor of this trunk commit", so a trunk that rewound would let a
    # stale answer claim the work is on trunk, and retiring on that abandons local commits
    # for work that is not there. Unlike merge, which compares no trunk commits, this is
    # load-bearing.
    if observation.fetched_trunk_commit_id != finish.trunk_commit_id:
        return None, t"fetched {ui.revset('trunk()')} changed while checking the merged PR"
    if observation.remote_trunk_target != finish.trunk_commit_id:
        return None, "the live trunk ref moved after the last fetch"
    review = observation.reviews[candidate.change_id]
    if (
        review.identity != candidate.review_identity
        or review.baseline != candidate.submitted_baseline
    ):
        return None, "saved PR tracking changed while checking the merged PR"
    return observation, None


async def retire_reviews(
    *,
    evidence: dict[str, TrunkEvidenceKind],
    finish_results: tuple[ReviewFinishResult, ...],
    finish: FinishContext,
    retirement_blocker: Callable[[TrackedReview], Message | None] | None = None,
    terminal_required: frozenset[str] = frozenset(),
) -> tuple[ReviewFinishResult, ...]:
    """Retire tracking independently, once each pull request has reached a terminal state."""

    context = finish.command

    async def current_retirement_blocker(
        candidate: TrackedReview,
    ) -> Message | None:
        blocker = retirement_blocker(candidate) if retirement_blocker is not None else None
        return blocker or await _fresh_retirement_blocker(
            candidate=candidate,
            evidence_kind=evidence[candidate.change_id],
            finish=finish,
            terminal_required=candidate.change_id in terminal_required,
        )

    results = list(finish_results)
    active_results = filter(lambda item: item[1].outcome != "skipped", enumerate(results))
    for index, result in active_results:
        candidate = result.candidate
        reason = await current_retirement_blocker(candidate)
        if reason is not None:
            results[index] = replace(result, retirement_skip_reason=reason)
            continue
        if not finish.dry_run:
            try:
                context.state_store.retire_review(
                    candidate.change_id,
                )
            except (OSError, RuntimeError, ValueError) as error:
                results[index] = replace(result, retirement_failure=error_message(error))
                continue
        results[index] = replace(
            result,
            retired_tracking=True,
        )
    return tuple(results)


async def _fresh_retirement_blocker(
    *,
    candidate: TrackedReview,
    evidence_kind: TrunkEvidenceKind,
    finish: FinishContext,
    terminal_required: bool,
) -> Message | None:
    local_revisions = finish.command.jj_client.query_revisions_by_change_ids(
        (candidate.change_id,)
    ).get(candidate.change_id, ())
    if any(
        revision.holds_unpublished_edit((candidate.submitted_baseline.commit_id,))
        for revision in local_revisions
    ):
        return t"{ui.change_id(candidate.change_id)} has unpublished local edits"
    if any(not revision.immutable for revision in local_revisions):
        return t"{ui.change_id(candidate.change_id)} still has an editable local revision"
    observation, reason = await observe_tracked_review(candidate, finish)
    if reason is not None or observation is None:
        return reason
    pull_request = observation.reviews[candidate.change_id].pull_request
    if pull_request is None:
        return t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
    if terminal_required and pull_request.normalize_state().state != "merged":
        return (
            t"PR #{pull_request.number} belongs to a GitHub stack and does not report merged yet"
        )
    exact, rewritten = collect_trunk_evidence(
        candidate=candidate,
        context=finish.command,
        pull_request=pull_request,
        repository=finish.github.repository,
        trunk_commit_id=finish.trunk_commit_id,
    )
    evidence = exact if evidence_kind == "exact" else rewritten
    return None if evidence.on_trunk else evidence.reason


def finish_exit_code(*, base: int, results: Sequence[ReviewFinishResult]) -> int:
    """Fold a failed tracking removal into a command's exit status.

    Preserving tracking a dependent stack still needs is ordinary operation and leaves the
    base status alone; a durable write that passed its checks and then failed is a failure
    a scripted caller has to be able to see.
    """

    return 1 if any(result.retirement_failure is not None for result in results) else base


def render_finish_results(
    *,
    dry_run: bool,
    results: tuple[ReviewFinishResult, ...],
) -> None:
    """Render the finish and retirement outcomes, which succeed or skip independently."""

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
        if result.outcome == "finished":
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
