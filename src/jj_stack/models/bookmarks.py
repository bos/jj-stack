"""Typed bookmark and remote models derived from `jj` state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GitRemote(BaseModel):
    """A configured Git remote known to the local `jj` repository."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str


class RemoteBookmarkState(BaseModel):
    """Observed state for one bookmark on one remote."""

    model_config = ConfigDict(frozen=True)

    remote: str
    targets: tuple[str, ...] = Field(default_factory=tuple)
    tracking_targets: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def is_tracked(self) -> bool:
        """Whether the local repo tracks this remote bookmark."""

        return bool(self.tracking_targets)

    @property
    def target(self) -> str | None:
        """Return the remote target when it is unambiguous."""

        if len(self.targets) != 1:
            return None
        return self.targets[0]


class BookmarkState(BaseModel):
    """Observed local and remote state for one bookmark name."""

    model_config = ConfigDict(frozen=True)

    local_targets: tuple[str, ...] = Field(default_factory=tuple)
    name: str
    remote_targets: tuple[RemoteBookmarkState, ...] = Field(default_factory=tuple)

    @property
    def local_target(self) -> str | None:
        """Return the local target when it is unambiguous."""

        if len(self.local_targets) != 1:
            return None
        return self.local_targets[0]

    def remote_target(self, remote: str) -> RemoteBookmarkState | None:
        """Return the observed state for the named remote, if present."""

        for candidate in self.remote_targets:
            if candidate.remote == remote:
                return candidate
        return None
