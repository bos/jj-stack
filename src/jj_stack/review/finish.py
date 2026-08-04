"""Finish reviews whose work is proven to be on trunk, then stop tracking them.

Finishing and untracking are kept separate: a pull request GitHub has ended is finished for good,
while its tracking has to survive until no local path still needs it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import error_message
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubPullRequest
from jj_stack.ui import Message

from .trunk_evidence import TrackedReview

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
    trunk_branch: str


async def finish_reviews(
    *,
    candidates: tuple[TrackedReview, ...],
    finish: FinishContext,
    pull_requests: Mapping[int, GithubPullRequest],
    skip_finish: frozenset[str] = frozenset(),
) -> tuple[ReviewFinishResult, ...]:
    """Finish the supplied reviews, reporting skipped ones as already terminal.

    Callers pass the PR snapshots that established each candidate's trunk evidence and name the
    ones GitHub has already finished.
    """

    return tuple(
        [
            ReviewFinishResult(candidate=candidate, outcome="already_terminal")
            if candidate.change_id in skip_finish
            else await _finish_review(
                candidate,
                finish,
                pull_requests[candidate.review_identity.pr_number],
            )
            for candidate in candidates
        ]
    )


async def _finish_review(
    candidate: TrackedReview,
    finish: FinishContext,
    pull_request: GithubPullRequest,
) -> ReviewFinishResult:
    pull_request = pull_request.normalize_state()
    if pull_request.state != "open":
        return ReviewFinishResult(candidate=candidate, outcome="already_terminal")
    if not finish.dry_run:
        console.output(t"Finishing PR #{pull_request.number} for {candidate.change_id}...")
        reason = await _finish_open_review(
            finish=finish,
            pull_request=pull_request,
        )
        if reason is not None:
            return ReviewFinishResult(candidate=candidate, outcome="skipped", skip_reason=reason)
    return ReviewFinishResult(candidate=candidate, outcome="finished")


async def _finish_open_review(
    *,
    finish: FinishContext,
    pull_request: GithubPullRequest,
) -> Message | None:
    try:
        if pull_request.base.ref != finish.trunk_branch:
            pull_request = (
                await finish.github.update_pull_request(
                    pull_number=pull_request.number,
                    base=finish.trunk_branch,
                )
            ).normalize_state()
            if pull_request.state != "open":
                return None
            if pull_request.base.ref != finish.trunk_branch:
                return t"PR #{pull_request.number} did not stay retargeted to trunk"
        await finish.github.close_pull_request(pull_number=pull_request.number)
    except GithubClientError as error:
        return t"could not finish cleanup for PR #{pull_request.number}: {error}"
    return None


def retire_reviews(
    *,
    finish_results: tuple[ReviewFinishResult, ...],
    finish: FinishContext,
    retirement_blocker: Callable[[TrackedReview], Message | None] | None = None,
) -> tuple[ReviewFinishResult, ...]:
    """Retire tracking independently, once each pull request has reached a terminal state."""

    context = finish.command

    def current_retirement_blocker(candidate: TrackedReview) -> Message | None:
        blocker = retirement_blocker(candidate) if retirement_blocker is not None else None
        return blocker or _local_retirement_blocker(
            candidate=candidate,
            finish=finish,
        )

    results = list(finish_results)
    active_results = filter(lambda item: item[1].outcome != "skipped", enumerate(results))
    for index, result in active_results:
        candidate = result.candidate
        reason = current_retirement_blocker(candidate)
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


def _local_retirement_blocker(
    *,
    candidate: TrackedReview,
    finish: FinishContext,
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
    return None


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
