"""Stable naming policy for jj-stack review branches."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalRevision

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


_prefix: str | None = None


def install_review_namespace(prefix: str) -> None:
    """Install the namespace resolved from config for the rest of this invocation.

    Unset until bootstrap installs it: this namespace decides which remote refs jj-stack may
    force-push or delete, so reading it early has to fail loudly rather than fall back to a
    default naming someone else's branches.
    """

    global _prefix
    _prefix = prefix


@contextmanager
def configured_review_namespace(prefix: str | None) -> Iterator[None]:
    """Scope the reserved namespace, restoring the previous one; `None` leaves it uninstalled."""

    global _prefix
    previous = _prefix
    _prefix = prefix
    try:
        yield
    finally:
        _prefix = previous


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
