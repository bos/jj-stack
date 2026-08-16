"""Shared representations of jj-stack identifiers."""


def short_change_id(change_id: str) -> str:
    """Return a stable short prefix for a full change ID."""

    return change_id[:8]
