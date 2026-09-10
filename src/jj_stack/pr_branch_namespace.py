"""Configured ownership policy for jj-stack PR branches."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jj_stack.identifiers import SHORT_CHANGE_ID_LENGTH, ChangeId, short_change_id
from jj_stack.models.stack import LocalCommit

_DEFAULT_SLUG = "change"
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# GitHub refuses a ref longer than 255 bytes, counting the "refs/heads/" prefix. A generated
# branch is "<prefix>/<slug>-<short change ID>" and the slug may shrink to nothing, so this is
# the most a configured prefix may take; whatever it leaves unused goes to the slug.
MAX_BRANCH_PREFIX_BYTES = 255 - len("refs/heads/") - len("/-") - SHORT_CHANGE_ID_LENGTH
_current_namespace: PRBranchNamespace | None = None


@dataclass(frozen=True, slots=True)
class PRBranchNamespace:
    """The configured namespace jj-stack may use for PR branches."""

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

    def generate_branch(self, change: LocalCommit) -> str:
        """Generate the initial readable branch name for a change."""

        first_line = change.description.splitlines()[0] if change.description else ""
        slug = _NON_ALNUM_RE.sub("-", first_line.lower()).strip("-") or _DEFAULT_SLUG
        suffix = f"-{short_change_id(change.change_id)}"
        # The slug is the only variable-length part, and both it and the suffix are ASCII,
        # so truncating the slug by characters keeps a long subject inside GitHub's limit
        # without disturbing the suffix that ties the branch to its change. The configured
        # prefix may hold non-ASCII characters, which GitHub counts as the bytes they encode.
        slug_budget = MAX_BRANCH_PREFIX_BYTES - len(self.prefix.encode())
        return f"{self.branch_prefix}{slug[:slug_budget].rstrip('-')}{suffix}"

    def contains(self, branch: str) -> bool:
        """Return whether a branch belongs to this namespace."""

        return branch.startswith(self.branch_prefix)


def install_pr_branch_namespace(prefix: str) -> None:
    """Install the configured namespace for the current CLI invocation."""

    global _current_namespace
    _current_namespace = PRBranchNamespace(prefix)


def current_pr_branch_namespace() -> PRBranchNamespace:
    """Return the namespace installed during command bootstrap."""

    if _current_namespace is None:
        raise RuntimeError("PR branch namespace has not been installed")
    return _current_namespace


def pr_branch_matches_change(branch: str, change_id: ChangeId) -> bool:
    """Whether a branch carries the change's short-ID suffix."""

    return branch.endswith(f"-{short_change_id(change_id)}")
