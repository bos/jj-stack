"""Configured ownership policy for jj-stack review branches."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jj_stack.identifiers import short_change_id
from jj_stack.models.stack import LocalRevision

_DEFAULT_SLUG = "change"
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_current_namespace: ReviewNamespace | None = None


@dataclass(frozen=True, slots=True)
class ReviewNamespace:
    """The configured namespace jj-stack may use for review branches."""

    prefix: str

    @property
    def branch_prefix(self) -> str:
        """Return the namespace as users see it, such as ``jj-stack/``."""

        return f"{self.prefix}/"

    @property
    def branch_glob(self) -> str:
        """Return the bookmark pattern matching every branch in the namespace."""

        return f"{self.branch_prefix}*"

    @property
    def fetch_refspec(self) -> str:
        """Return the negative Git refspec that excludes the namespace from fetch."""

        return f"^refs/heads/{self.branch_glob}"

    def generate_branch(self, revision: LocalRevision) -> str:
        """Generate the initial readable branch name for a change."""

        first_line = revision.description.splitlines()[0] if revision.description else ""
        slug = _NON_ALNUM_RE.sub("-", first_line.lower()).strip("-") or _DEFAULT_SLUG
        return f"{self.branch_prefix}{slug}-{short_change_id(revision.change_id)}"

    def contains(self, branch: str) -> bool:
        """Return whether a branch belongs to this namespace."""

        return branch.startswith(self.branch_prefix)

    def branch_ref(self, branch: str) -> str:
        """Return the full Git ref for one branch, refusing anything outside."""

        if not self.contains(branch):
            raise ValueError(f"not a branch in the {self.branch_prefix} namespace: {branch!r}")
        return f"refs/heads/{branch}"


def install_review_namespace(prefix: str) -> None:
    """Install the configured namespace for the current CLI invocation."""

    global _current_namespace
    _current_namespace = ReviewNamespace(prefix)


def current_review_namespace() -> ReviewNamespace:
    """Return the namespace installed during command bootstrap."""

    if _current_namespace is None:
        raise RuntimeError("review namespace has not been installed")
    return _current_namespace


def review_branch_matches_change(branch: str, change_id: str) -> bool:
    """Whether a branch carries the change's short-ID suffix."""

    return branch.endswith(f"-{short_change_id(change_id)}")
