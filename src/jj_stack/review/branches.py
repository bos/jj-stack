"""Stable naming policy for jj-stack review branches."""

from __future__ import annotations

import re

from jj_stack.formatting import short_change_id
from jj_stack.models.stack import LocalRevision

REVIEW_BRANCH_PREFIX = "review"

_DEFAULT_SLUG = "change"
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_RESTART_MARKER_RE = re.compile(r"(?:^|-)fresh-pr(?P<number>[1-9]\d*)$")
_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHORT_ID_RE = re.compile(r"[a-z0-9]{8}")


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
    if _RESTART_MARKER_RE.search(slug) is not None:
        slug = f"{slug}-change"
    return f"{REVIEW_BRANCH_PREFIX}/{slug}-{short_change_id(revision.change_id)}"


def restarted_review_branch(
    *,
    change_id: str,
    previous_branch: str,
    previous_pull_request: int,
) -> str:
    """Derive a retry-stable fresh branch solely from saved review identity."""

    parts = _managed_branch_parts(previous_branch)
    if parts is None or parts[1] != short_change_id(change_id):
        raise ValueError("saved review branch does not match the change ID")
    base_stem, short_id = parts
    return f"{REVIEW_BRANCH_PREFIX}/{base_stem}-fresh-pr{previous_pull_request}-{short_id}"


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
    marker = _RESTART_MARKER_RE.search(stem)
    base_stem = stem[: marker.start()] if marker is not None else stem
    if not base_stem or _RESTART_MARKER_RE.search(base_stem) is not None:
        return None
    return base_stem, suffix
