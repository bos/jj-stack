"""Typed local stack models derived from `jj` state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LocalRevision(BaseModel):
    """A commit with the fields needed for stack discovery."""

    model_config = ConfigDict(frozen=True)

    change_id: str
    commit_id: str
    conflict: bool = False
    current_working_copy: bool
    description: str
    divergent: bool
    empty: bool
    hidden: bool
    immutable: bool
    parents: tuple[str, ...]

    @property
    def subject(self) -> str:
        """Return the first non-empty description line for display."""

        first_line = self.description.splitlines()[0] if self.description else ""
        return first_line or "(no description set)"

    def is_reviewable(
        self,
        *,
        allow_divergent: bool = False,
        allow_immutable: bool = False,
    ) -> bool:
        """Whether the revision should count as a review change."""

        return (
            not self.hidden
            and (allow_immutable or not self.immutable)
            and (allow_divergent or not self.divergent)
            and not (self.current_working_copy and self.empty)
            and len(self.parents) == 1
        )

    def only_parent_commit_id(self) -> str:
        """Return the sole parent commit ID when the revision is linear."""

        if len(self.parents) != 1:
            raise ValueError("Revision does not have exactly one parent.")
        return self.parents[0]


class LocalStack(BaseModel):
    """A linear stack of reviewable revisions with explicit trunk and base-parent context."""

    model_config = ConfigDict(frozen=True)

    base_parent: LocalRevision
    base_parent_is_trunk_ancestor: bool = False
    head: LocalRevision
    revisions: tuple[LocalRevision, ...]
    selected_revset: str
    trunk: LocalRevision
