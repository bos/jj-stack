"""Finish reviews whose work is proven to be on trunk."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubPullRequest
from jj_stack.ui import Message

from .trunk_evidence import TrackedReview

ReviewFinishOutcome = Literal["finished", "already_terminal", "skipped"]


@dataclass(frozen=True, slots=True)
class ReviewFinishResult:
    candidate: TrackedReview
    outcome: ReviewFinishOutcome
    skip_reason: Message | None = None


@dataclass(frozen=True, slots=True)
class FinishContext:
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


def render_finish_results(
    *,
    dry_run: bool,
    results: tuple[ReviewFinishResult, ...],
) -> None:
    """Render the GitHub updates, omitting reviews that were already terminal."""

    visible_results = tuple(result for result in results if result.outcome != "already_terminal")
    if not visible_results:
        return
    console.output(
        "Planned GitHub updates for merged PRs:"
        if dry_run
        else "Applied GitHub updates for merged PRs:"
    )
    marker = "•" if dry_run else "✓"
    for result in visible_results:
        candidate = result.candidate
        if result.outcome == "skipped":
            console.output(
                t"  ! leave {ui.change_id(candidate.change_id)} unchanged: {result.skip_reason}"
            )
            continue
        if result.outcome == "finished":
            console.output(t"  {marker} finish merged PR #{candidate.review_identity.pr_number}")
