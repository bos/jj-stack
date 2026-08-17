"""Typed Git models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GitRemote(BaseModel):
    """A configured Git remote known to the local `jj` repo."""

    model_config = ConfigDict(frozen=True)

    name: str
    fetch_url: str
    push_url: str
