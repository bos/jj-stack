"""Stable naming policy for jj-stack PR branches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import PRIdentity, TrackingState
from jj_stack.pr_branch_namespace import current_pr_branch_namespace

if TYPE_CHECKING:
    from jj_stack.jj.client import JjClient


@dataclass(frozen=True, slots=True)
class ResolvedPRBranch:
    """Stable PR branch selected for one local change."""

    branch: str
    change_id: str
    recovered_target: str | None = None


def prepare_visible_pr_snapshots(
    *,
    jj_client: JjClient,
    state: TrackingState,
) -> None:
    """Observe saved PR bookmarks and narrow built-in bookmark immutability."""

    visible = jj_client.visible_pr_bookmark_targets()
    claims: dict[str, list[tuple[str, str]]] = {}
    for tracked_pr in state.tracked_prs():
        branch = tracked_pr.pr_identity.head_ref
        baseline = tracked_pr.submitted_baseline.commit_id
        if visible.get(branch) == frozenset({baseline}):
            claims.setdefault(branch, []).append((tracked_pr.change_id, baseline))
    exact = {branch: items[0] for branch, items in claims.items() if len(items) == 1}
    jj_client.accept_expected_pr_bookmarks(
        tuple((branch, change_id, commit_id) for branch, (change_id, commit_id) in exact.items())
    )


def resolve_pr_branches(
    *,
    changes: tuple[LocalCommit, ...],
    pr_identities: Mapping[str, PRIdentity],
) -> tuple[ResolvedPRBranch, ...]:
    """Resolve each branch from its saved identity or initial name."""

    resolutions = tuple(
        ResolvedPRBranch(
            branch=(
                identity.head_ref
                if (identity := pr_identities.get(change.change_id)) is not None
                else current_pr_branch_namespace().generate_branch(change)
            ),
            change_id=change.change_id,
        )
        for change in changes
    )
    ensure_unique_pr_branches(resolutions)
    return resolutions


def ensure_new_pr_branches_unclaimed(
    resolutions: tuple[ResolvedPRBranch, ...],
    pr_identities: Mapping[str, PRIdentity],
    repo_key: tuple[str, str],
) -> None:
    saved_by_branch = {
        identity.head_ref: change_id
        for change_id, identity in pr_identities.items()
        if identity.repo_key == repo_key
    }
    collisions = tuple(
        resolution.branch
        for resolution in resolutions
        if resolution.change_id not in pr_identities
        and resolution.branch in saved_by_branch
        and saved_by_branch[resolution.branch] != resolution.change_id
    )
    if collisions:
        raise CliError(
            t"Cannot create a pull request on saved PR branch "
            t"{ui.join(ui.bookmark, collisions)}.",
            hint=t"Run {ui.cmd('jj-stack list')} to find the change that owns it, then clean "
            t"up that pull request or change the new change's subject.",
        )


def ensure_unique_pr_branches(
    resolutions: tuple[ResolvedPRBranch, ...],
) -> None:
    duplicates = duplicate_pr_branch_claims(
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
        hint="Change an untracked change's subject or repair the saved pull request links.",
    )


def duplicate_pr_branch_claims(
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
