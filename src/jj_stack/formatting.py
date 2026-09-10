"""Shared output-formatting helpers."""

from __future__ import annotations

import sys
from typing import Protocol

import jj_stack.ui as ui
from jj_stack.console import color_when
from jj_stack.identifiers import CommitId
from jj_stack.jj.colors import JjColorWhen


class RenderableCommit(Protocol):
    """Change-like value that can be rendered by commit ID."""

    @property
    def commit_id(self) -> CommitId: ...


class CommitRenderClient(Protocol):
    """Subset of the jj client interface used for change rendering."""

    def render_commit_log_blocks(
        self,
        changes: tuple[RenderableCommit, ...],
        *,
        color_when: JjColorWhen,
    ) -> dict[CommitId, tuple[str, ...]]: ...


class GithubRepoRenderTarget(Protocol):
    """GitHub repo coordinates needed to render a pull request URL."""

    @property
    def full_name(self) -> str: ...


def pr_url(
    pr_number: int,
    *,
    repo: GithubRepoRenderTarget | None,
    url: str | None,
) -> str | None:
    return url or (f"https://github.com/{repo.full_name}/pull/{pr_number}" if repo else None)


def format_pr_number(
    pr_number: int,
    *,
    repo: GithubRepoRenderTarget | None = None,
    url: str | None = None,
) -> ui.Message:
    """Render a pull request number, linking it when its repo or URL is known."""

    url = pr_url(pr_number, repo=repo, url=url)
    text = f"#{pr_number}"
    return ui.hyperlink(text, url) if url is not None else text


def format_pr_label(
    pr_number: int,
    *,
    include_hash: bool = True,
    is_draft: bool = False,
    prefix: str = "",
    repo: GithubRepoRenderTarget | None = None,
    url: str | None = None,
) -> ui.Message:
    """Render a pull request label for CLI output."""

    url = pr_url(pr_number, repo=repo, url=url)
    text = f"PR {'#' if include_hash else ''}{pr_number}"
    label: ui.Message = ui.hyperlink(text, url) if url is not None else text
    if is_draft:
        label = ("draft ", label)
    return (prefix, label) if prefix else label


def render_commit_lines(
    raw_lines: tuple[str, ...],
    *,
    suffix: ui.Message | None = None,
) -> tuple[ui.Renderable, ...]:
    """Add an optional status suffix to a rendered `jj log` block."""

    if suffix is None:
        return raw_lines
    return (ui.suffixed_line(raw_lines[0], suffix), *raw_lines[1:])


def render_commit_blocks(
    *,
    client: CommitRenderClient,
    changes: tuple[RenderableCommit, ...],
) -> dict[CommitId, tuple[str, ...]]:
    """Render several changes using the active CLI/UI color policy."""

    if not changes:
        return {}
    return client.render_commit_log_blocks(
        changes, color_when=color_when(stdout_is_tty=sys.stdout.isatty())
    )
