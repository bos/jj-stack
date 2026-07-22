"""Shared planning for starting fresh review tracking."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.config import RepoConfig
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.bookmarks import (
    bookmark_matches_restart_change_id,
    generate_bookmark_name,
)


@dataclass(frozen=True, slots=True)
class RestartedChange:
    """One local change whose saved review identity was reset."""

    change_id: str
    new_bookmark: str
    old_bookmark: str | None
    old_pr_number: int | None
    subject: str


@dataclass(frozen=True, slots=True)
class RestartedReview:
    """One exact saved pair to retire before starting a fresh review."""

    baseline: SubmittedBaseline
    change: RestartedChange
    commit_id: str
    identity: ReviewIdentity


@dataclass(frozen=True, slots=True)
class RestartStateResult:
    """Tracking state prepared for fresh pull requests."""

    restarted: tuple[RestartedReview, ...]
    state: ReviewState

    @property
    def changed(self) -> tuple[RestartedChange, ...]:
        """Return the user-facing restart descriptions."""

        return tuple(item.change for item in self.restarted)


def restart_state_for_stack(
    *,
    bookmark_states: dict[str, BookmarkState],
    config: RepoConfig,
    reserved_bookmarks: Iterable[str] = (),
    stack: LocalStack,
    state: ReviewState,
) -> RestartStateResult:
    """Return tracking state where selected submitted changes will use fresh PRs."""

    _ensure_stack_has_no_unlinked_changes(stack=stack, state=state)
    used_bookmarks = {
        *(identity.head_ref for identity in state.review_identities.values()),
        *bookmark_states,
        *reserved_bookmarks,
    }

    identities = dict(state.review_identities)
    baselines = dict(state.submitted_baselines)
    restarted: list[RestartedReview] = []
    for revision in stack.revisions:
        if state.issues_for(revision.change_id):
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is malformed.",
                hint=t"Repair it with {ui.cmd('relink')} before restarting the review.",
            )
        identity = state.review_identities.get(revision.change_id)
        baseline = state.submitted_baselines.get(revision.change_id)
        if identity is None and baseline is None:
            continue
        if identity is None or baseline is None:
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is incomplete.",
                hint=t"Repair it with {ui.cmd('relink')} before restarting the review.",
            )
        new_bookmark = _existing_fresh_bookmark(
            bookmark_states=bookmark_states,
            config=config,
            old_bookmark=identity.head_ref,
            revision=revision,
        ) or fresh_bookmark_name(
            config=config,
            revision=revision,
            old_bookmark=identity.head_ref,
            old_pr_number=identity.pr_number,
            used_bookmarks=used_bookmarks,
        )
        used_bookmarks.add(new_bookmark)
        identities.pop(revision.change_id)
        baselines.pop(revision.change_id)
        restarted.append(
            RestartedReview(
                baseline=baseline,
                change=RestartedChange(
                    change_id=revision.change_id,
                    new_bookmark=new_bookmark,
                    old_bookmark=identity.head_ref,
                    old_pr_number=identity.pr_number,
                    subject=revision.subject,
                ),
                commit_id=revision.commit_id,
                identity=identity,
            )
        )

    return RestartStateResult(
        restarted=tuple(restarted),
        state=ReviewState(
            review_identities=identities,
            submitted_baselines=baselines,
            record_issues=state.record_issues,
        ),
    )


def _existing_fresh_bookmark(
    *,
    bookmark_states: dict[str, BookmarkState],
    config: RepoConfig,
    old_bookmark: str,
    revision: LocalRevision,
) -> str | None:
    candidates = sorted(
        bookmark
        for bookmark, bookmark_state in bookmark_states.items()
        if bookmark != old_bookmark
        and bookmark_matches_restart_change_id(
            bookmark,
            revision.change_id,
            prefix=config.bookmark_prefix,
        )
        and bookmark_state.local_target == revision.commit_id
        and not bookmark_state.remote_targets
    )
    if len(candidates) > 1:
        raise CliError(
            t"Could not safely resume restart for {ui.change_id(revision.change_id)}: "
            t"multiple fresh local bookmarks exist: {ui.join(ui.bookmark, candidates)}."
        )
    return candidates[0] if candidates else None


def fresh_bookmark_name(
    *,
    config: RepoConfig,
    old_bookmark: str | None,
    old_pr_number: int | None,
    revision: LocalRevision,
    used_bookmarks: set[str],
) -> str:
    base = generate_bookmark_name(revision, prefix=config.bookmark_prefix)
    short_id = short_change_id(revision.change_id)
    suffix = f"-{short_id}"
    stem = base[: -len(suffix)] if base.endswith(suffix) else base
    markers = _fresh_markers(old_pr_number=old_pr_number)
    for marker in markers:
        candidate = f"{stem}-{marker}-{short_id}"
        if candidate == old_bookmark or candidate in used_bookmarks:
            continue
        return candidate
    raise CliError(
        t"Could not choose a fresh review bookmark for {ui.change_id(revision.change_id)}."
    )


def _fresh_markers(*, old_pr_number: int | None) -> Iterable[str]:
    if old_pr_number is not None:
        yield f"fresh-pr{old_pr_number}"
    yield "fresh"
    for attempt in range(2, 100):
        yield f"fresh-{attempt}"


def _ensure_stack_has_no_unlinked_changes(
    *,
    stack: LocalStack,
    state: ReviewState,
) -> None:
    unlinked = tuple(
        revision
        for revision in stack.revisions
        if (identity := state.review_identities.get(revision.change_id)) is not None
        and identity.is_unlinked
    )
    if not unlinked:
        return
    if len(unlinked) == 1:
        revision = unlinked[0]
        raise CliError(
            t"Change {ui.change_id(revision.change_id)} is unlinked from review tracking.",
            hint=t"Use {ui.cmd('relink')} if it should be attached to review again.",
        )
    raise CliError(
        t"Selected stack contains unlinked changes: "
        t"{ui.join(lambda revision: ui.change_id(revision.change_id), unlinked)}.",
        hint=t"Use {ui.cmd('relink')} for changes that should be attached to review again.",
    )
