"""Shared output-formatting helpers."""

from __future__ import annotations

import sys
from typing import IO, Literal, Protocol

from jj_stack.console import RequestedColorMode, requested_color_mode


class RenderableRevision(Protocol):
    """Revision-like value that can be rendered by commit ID."""

    @property
    def commit_id(self) -> str: ...


class RevisionRenderClient(Protocol):
    """Subset of the jj client interface used for revision rendering."""

    def resolve_color_when(
        self,
        *,
        cli_color: RequestedColorMode | None = None,
        stdout_is_tty: bool,
    ) -> Literal["always", "debug", "never"]: ...

    def render_revision_log_lines(
        self,
        revision: RenderableRevision,
        *,
        color_when: Literal["always", "debug", "never"],
    ) -> tuple[str, ...]: ...

    def render_revision_log_blocks(
        self,
        revisions: tuple[RenderableRevision, ...],
        *,
        color_when: Literal["always", "debug", "never"],
    ) -> dict[str, tuple[str, ...]]: ...


def format_pull_request_label(
    pull_request_number: int,
    *,
    is_draft: bool = False,
    prefix: str = "",
) -> str:
    """Render a pull request label for CLI output."""

    label = f"PR #{pull_request_number}"
    if is_draft:
        label = f"draft {label}"
    return f"{prefix}{label}"


def render_revision_lines(
    *,
    client: RevisionRenderClient,
    revision: RenderableRevision,
    stdout: IO[str] | None = None,
    suffix: str | None = None,
    prerendered_lines: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Render one revision using the active CLI/UI color policy."""

    if prerendered_lines is None:
        stream = sys.stdout if stdout is None else stdout
        color_when = client.resolve_color_when(
            cli_color=requested_color_mode(),
            stdout_is_tty=stream.isatty(),
        )
        raw_lines = client.render_revision_log_lines(revision, color_when=color_when)
    else:
        raw_lines = prerendered_lines
    lines = list(raw_lines)
    if not lines:
        raise AssertionError("Expected `jj log` to render at least one line for a revision.")
    if suffix is not None:
        lines[0] = f"{lines[0]}: {suffix}"
    return tuple(lines)


def render_revision_blocks(
    *,
    client: RevisionRenderClient,
    revisions: tuple[RenderableRevision, ...],
    stdout: IO[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Render several revisions using the active CLI/UI color policy."""

    if not revisions:
        return {}
    stream = sys.stdout if stdout is None else stdout
    color_when = client.resolve_color_when(
        cli_color=requested_color_mode(),
        stdout_is_tty=stream.isatty(),
    )
    return client.render_revision_log_blocks(revisions, color_when=color_when)
