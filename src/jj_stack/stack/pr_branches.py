"""Stable naming policy for jj-stack PR branches."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import PRIdentity, TrackedPR
from jj_stack.pr_branch_namespace import current_pr_branch_namespace


@dataclass(frozen=True, slots=True)
class ResolvedPRBranch:
    """Stable PR branch selected for one local change."""

    branch: str
    change_id: str
    recovered: bool = False


def resolve_pr_branches(
    *,
    changes: tuple[LocalCommit, ...],
    tracked_prs: Mapping[str, TrackedPR],
) -> tuple[ResolvedPRBranch, ...]:
    """Resolve each branch from its saved identity or initial name."""

    resolutions = tuple(
        ResolvedPRBranch(
            branch=(
                tracked.pr_identity.head_ref
                if (tracked := tracked_prs.get(change.change_id)) is not None
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
    tracked_prs: Mapping[str, TrackedPR],
) -> None:
    saved_by_branch = {
        tracked.pr_identity.head_ref: change_id for change_id, tracked in tracked_prs.items()
    }
    collisions = tuple(
        resolution.branch
        for resolution in resolutions
        if resolution.change_id not in tracked_prs
        and resolution.branch in saved_by_branch
        and saved_by_branch[resolution.branch] != resolution.change_id
    )
    if collisions:
        raise CliError(
            t"Cannot create a pull request: these PR branches are already linked to other "
            t"changes: "
            t"{ui.join(ui.bookmark, collisions)}.",
            hint=t"Run {ui.cmd('jj-stack list')} to find those changes. Use "
            t"{ui.cmd('jj-stack cleanup --pull-request PR')} for a closed or merged PR, or "
            t"change the new change's subject with {ui.cmd('jj describe CHANGE')}.",
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
        t"Multiple changes in the selected stack would use the same PR branch: {collisions}.",
        hint=t"Use {ui.cmd('jj describe CHANGE')} to change an unsubmitted change's subject, or "
        t"{ui.cmd('jj-stack relink PR CHANGE')} to correct a saved pull request link.",
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


def duplicate_pr_claim_change_ids(identities: Mapping[str, PRIdentity]) -> frozenset[str]:
    """Return every change participating in a duplicate PR or head claim."""

    values = identities.values()
    pr_claims = Counter(item.pr_number for item in values)
    head_claims = Counter(item.head_ref for item in values)
    return frozenset(
        change_id
        for change_id, item in identities.items()
        if pr_claims[item.pr_number] > 1 or head_claims[item.head_ref] > 1
    )


def require_unique_pr_claims(
    *, saved: Mapping[str, PRIdentity], replacements: Mapping[str, PRIdentity]
) -> None:
    """Refuse saved links that would give one PR number or branch two local changes."""

    combined = {**saved, **replacements}
    claimed = sorted(duplicate_pr_claim_change_ids(combined).intersection(replacements))
    if not claimed:
        return
    labels = ui.join(
        lambda change_id: (
            t"{format_pr_label(combined[change_id].pr_number)} or branch "
            t"{ui.bookmark(combined[change_id].head_ref)}"
        ),
        claimed,
    )
    if len(claimed) == 1:
        message = t"{labels} is already linked to another local change."
    else:
        message = t"{labels} are already linked to other local changes."
    raise CliError(
        message,
        hint=t"Run {ui.cmd('jj-stack list')} to find the linked change. To forget its "
        t"stack's saved links, run {ui.cmd('jj-stack unstack --local <change-id>')}. "
        t"For a closed or merged PR, use {ui.cmd('jj-stack cleanup --pull-request <pr>')}.",
    )
