"""Stable naming policy for jj-stack review branches."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.models.stack import LocalRevision

if TYPE_CHECKING:
    from jj_stack.jj.client import JjClient

_DEFAULT_SLUG = "change"
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ResolvedReviewBranch:
    """Stable review branch selected for one local change."""

    branch: str
    change_id: str
    recovered_target: str | None = None


def review_branch_matches_change(branch: str, change_id: str) -> bool:
    """Whether a branch carries the change's short-ID suffix.

    This ties a branch to one logical change and asserts nothing else about the name. Whether a
    branch belongs to the reserved namespace is the namespace's own question, asked where a branch
    is adopted rather than re-derived from the name everywhere one is read.
    """

    return branch.endswith(f"-{short_change_id(change_id)}")


def prepare_visible_review_snapshots(
    *,
    jj_client: JjClient,
    state: ReviewState,
) -> None:
    """Observe saved review bookmarks and narrow built-in bookmark immutability."""

    visible = jj_client.visible_review_bookmark_targets()
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


def generate_review_branch(revision: LocalRevision) -> str:
    """Generate the initial readable branch name for a change."""

    first_line = revision.description.splitlines()[0] if revision.description else ""
    slug = _NON_ALNUM_RE.sub("-", first_line.lower()).strip("-") or _DEFAULT_SLUG
    return f"{review_namespace()}{slug}-{short_change_id(revision.change_id)}"


def resolve_review_branches(
    *,
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
                    else generate_review_branch(revision)
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


_prefix: str | None = None


def install_review_namespace(prefix: str) -> None:
    """Install the namespace resolved from config for the rest of this invocation.

    Unset until bootstrap installs it: this namespace decides which remote refs jj-stack may
    force-push or delete, so reading it early has to fail loudly rather than fall back to a
    default naming someone else's branches.
    """

    global _prefix
    _prefix = prefix


def review_namespace() -> str:
    """The reserved namespace as users see it, such as `jj-stack/`."""

    if _prefix is None:
        raise RuntimeError(
            "The reserved review namespace was read before bootstrap installed it."
        )
    return f"{_prefix}/"


def review_branch_glob() -> str:
    """The bookmark pattern matching every branch in the reserved namespace."""

    return f"{review_namespace()}*"


def review_fetch_refspec() -> str:
    """The negative Git refspec that keeps the namespace out of ordinary fetch."""

    return f"^refs/heads/{review_branch_glob()}"


def is_review_branch(branch: str) -> bool:
    """Whether a branch is in the reserved namespace."""

    return branch.startswith(review_namespace())


def review_branch_ref(branch: str) -> str:
    """Return the full Git ref for one review branch, refusing anything outside.

    Remote ref reads, force-pushes, and deletions all go through here, so a branch that escaped
    the reservation must never reach the remote as a jj-stack-owned ref.
    """

    if not is_review_branch(branch):
        raise ValueError(f"not a branch in the {review_namespace()} namespace: {branch!r}")
    return f"refs/heads/{branch}"
