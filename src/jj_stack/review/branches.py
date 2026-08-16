"""Stable naming policy for jj-stack review branches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review_namespace import ReviewNamespace

if TYPE_CHECKING:
    from jj_stack.jj.client import JjClient


@dataclass(frozen=True, slots=True)
class ResolvedReviewBranch:
    """Stable review branch selected for one local change."""

    branch: str
    change_id: str
    recovered_target: str | None = None


def prepare_visible_review_snapshots(
    *,
    jj_client: JjClient,
    namespace: ReviewNamespace,
    state: ReviewState,
) -> None:
    """Observe saved review bookmarks and narrow built-in bookmark immutability."""

    visible = jj_client.visible_review_bookmark_targets(namespace=namespace)
    claims: dict[str, list[tuple[str, str]]] = {}
    for review in state.tracked_reviews():
        branch = review.review_identity.head_ref
        baseline = review.submitted_baseline.commit_id
        if visible.get(branch) == frozenset({baseline}):
            claims.setdefault(branch, []).append((review.change_id, baseline))
    exact = {branch: items[0] for branch, items in claims.items() if len(items) == 1}
    jj_client.accept_expected_review_bookmarks(
        tuple((branch, change_id, commit_id) for branch, (change_id, commit_id) in exact.items())
    )


def resolve_review_branches(
    *,
    namespace: ReviewNamespace,
    revisions: tuple[LocalRevision, ...],
    review_identities: Mapping[str, ReviewIdentity],
    overrides: Mapping[str, str] | None = None,
) -> tuple[ResolvedReviewBranch, ...]:
    """Resolve each branch from its saved identity, override, or initial name."""

    forced = overrides or {}
    resolutions = tuple(
        ResolvedReviewBranch(
            branch=(
                forced[revision.change_id]
                if revision.change_id in forced
                else (
                    identity.head_ref
                    if (identity := review_identities.get(revision.change_id)) is not None
                    else namespace.generate_branch(revision)
                )
            ),
            change_id=revision.change_id,
        )
        for revision in revisions
    )
    ensure_unique_review_branches(resolutions)
    return resolutions


def ensure_new_review_branches_unclaimed(
    resolutions: tuple[ResolvedReviewBranch, ...],
    review_identities: Mapping[str, ReviewIdentity],
    repository_key: tuple[str, str],
) -> None:
    saved_by_branch = {
        identity.head_ref: change_id
        for change_id, identity in review_identities.items()
        if identity.repository_key == repository_key
    }
    collisions = tuple(
        resolution.branch
        for resolution in resolutions
        if resolution.change_id not in review_identities
        and resolution.branch in saved_by_branch
        and saved_by_branch[resolution.branch] != resolution.change_id
    )
    if collisions:
        raise CliError(
            t"Cannot create a review on saved branch {ui.join(ui.bookmark, collisions)}.",
            hint=t"Run {ui.cmd('jj-stack list')} to find the change that owns it, then clean "
            t"up that review or change the new change's subject.",
        )


def ensure_unique_review_branches(
    resolutions: tuple[ResolvedReviewBranch, ...],
) -> None:
    duplicates = duplicate_review_branch_claims(
        (resolution.branch, resolution.change_id) for resolution in resolutions
    )
    if not duplicates:
        return
    collisions = ui.join(
        lambda item: t"{ui.bookmark(item[0])} for changes {ui.join(ui.change_id, item[1])}",
        sorted(duplicates.items()),
    )
    raise CliError(
        t"Selected stack resolves multiple changes to the same branch: {collisions}.",
        hint="Change an untracked change's subject or repair the saved review links.",
    )


def duplicate_review_branch_claims(
    claims: Iterable[tuple[str, str]],
) -> dict[str, tuple[str, ...]]:
    """Return branches claimed by more than one distinct change."""

    change_ids_by_branch: dict[str, set[str]] = {}
    for branch, change_id in claims:
        change_ids_by_branch.setdefault(branch, set()).add(change_id)
    return {
        branch: tuple(sorted(change_ids))
        for branch, change_ids in change_ids_by_branch.items()
        if len(change_ids) > 1
    }
