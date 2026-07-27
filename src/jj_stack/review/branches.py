"""Stable naming policy for jj-stack review branches."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalRevision

REVIEW_BRANCH_PREFIX = "review"

_DEFAULT_SLUG = "change"
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHORT_ID_RE = re.compile(r"[a-z0-9]{8}")


@dataclass(frozen=True, slots=True)
class ResolvedReviewBranch:
    """Stable review branch selected for one local change."""

    branch: str
    change_id: str
    recovered_target: str | None = None


def review_branch_glob() -> str:
    """Return the remote namespace reserved for jj-stack review branches."""

    return f"{REVIEW_BRANCH_PREFIX}/*"


def is_review_branch(branch: str) -> bool:
    """Whether a branch is in jj-stack's reserved review namespace."""

    return branch.startswith(f"{REVIEW_BRANCH_PREFIX}/")


def is_managed_review_branch(branch: str) -> bool:
    """Whether a branch has jj-stack's complete managed naming grammar."""

    return _managed_branch_parts(branch) is not None


def review_branch_matches_change(branch: str, change_id: str) -> bool:
    """Whether a branch has the managed grammar and the change's short-ID suffix."""

    parts = _managed_branch_parts(branch)
    return parts is not None and parts[1] == short_change_id(change_id)


def generate_review_branch(revision: LocalRevision) -> str:
    """Generate the initial readable branch name for a change."""

    first_line = revision.description.splitlines()[0] if revision.description else ""
    slug = _NON_ALNUM_RE.sub("-", first_line.lower()).strip("-") or _DEFAULT_SLUG
    return f"{REVIEW_BRANCH_PREFIX}/{slug}-{short_change_id(revision.change_id)}"


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


def ensure_unique_review_branches(
    resolutions: tuple[ResolvedReviewBranch, ...],
) -> None:
    claims: dict[str, list[str]] = {}
    for resolution in resolutions:
        claims.setdefault(resolution.branch, []).append(resolution.change_id)
    duplicates = {
        branch: change_ids for branch, change_ids in claims.items() if len(change_ids) > 1
    }
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


def _managed_branch_parts(branch: str) -> tuple[str, str] | None:
    """Return the readable base stem and short ID for one managed branch."""

    if not is_review_branch(branch):
        return None
    name = branch.removeprefix(f"{REVIEW_BRANCH_PREFIX}/")
    stem, separator, suffix = name.rpartition("-")
    if (
        separator != "-"
        or _SHORT_ID_RE.fullmatch(suffix) is None
        or _SLUG_RE.fullmatch(stem) is None
    ):
        return None
    return stem, suffix
