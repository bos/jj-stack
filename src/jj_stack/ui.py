"""Semantic message fragments shared across layers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from string.templatelib import Interpolation, Template, convert
from typing import Literal

from jj_stack.identifiers import short_change_id, short_commit_id


@dataclass(frozen=True, slots=True)
class SemanticText:
    """A short semantic text fragment that should preserve its semantic labels."""

    text: str
    labels: tuple[str, ...]
    link: str | None = None

    def __str__(self) -> str:
        return self.text


StatusValue = Literal["ok", "warn", "fail", "fixed", "skip"]
type Message = str | Template | SemanticText | tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class StatusBadge:
    """A semantic status indicator rendered by the console layer."""

    value: StatusValue


@dataclass(frozen=True, slots=True)
class PrefixedLine:
    """A hanging-indent line with semantic prefix and body content."""

    prefix: Message
    body: Message | StatusBadge
    message_labels: tuple[str, ...] | None = None
    prefix_labels: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class SuffixedLine:
    """An ANSI-rendered line followed by a semantic message."""

    body: str
    suffix: Message


type TableCell = Message | StatusBadge | PrefixedLine


@dataclass(frozen=True, slots=True)
class TableColumn:
    """A presentation-layer table column description."""

    header: str
    no_wrap: bool = False
    width: int | None = None


@dataclass(frozen=True, slots=True)
class DataTable:
    """A lightweight table model rendered by the console layer."""

    columns: tuple[TableColumn, ...]
    rows: tuple[tuple[TableCell, ...], ...]
    box: str = "simple"
    padding: int | tuple[int, int] | tuple[int, int, int, int] = (0, 0)
    show_header: bool = True


type Renderable = TableCell | DataTable | SuffixedLine


def semantic_text(text: str, *labels: str) -> SemanticText:
    """Wrap text with semantic labels for later rendering."""

    return SemanticText(text=text, labels=labels)


def hyperlink(text: str, url: str) -> SemanticText:
    """Wrap compact text with a terminal hyperlink target."""

    return SemanticText(text=text, labels=(), link=url)


def bookmark(name: str) -> SemanticText:
    """Wrap bookmark-like names, including Git remotes, for semantic rendering."""

    label = "remote_bookmarks" if "@" in name else "local_bookmarks"
    return semantic_text(name, label)


def change_id(name: str) -> SemanticText:
    """Wrap a change ID for semantic rendering, shortening it for display."""

    return semantic_text(short_change_id(name), "change_id")


def commit_id(name: str) -> SemanticText:
    """Wrap a commit ID for semantic rendering, shortening it for display."""

    return semantic_text(short_commit_id(name), "commit_id")


def revset(text: str) -> SemanticText:
    """Wrap jj revset syntax for semantic rendering."""

    return semantic_text(text, "revset")


def metavar(text: str) -> SemanticText:
    """Wrap a command-line metavariable."""

    return semantic_text(text, "metavar")


def code(text: str) -> SemanticText:
    """Wrap a code-like token for semantic rendering."""

    return semantic_text(text, "code")


def cmd(text: str) -> SemanticText:
    """Wrap a command-line snippet for semantic rendering.

    This can be used for commands, options, or arguments.
    """

    return semantic_text(text, "command", "hint")


def option(text: str) -> SemanticText:
    """Wrap one command-line option."""

    return semantic_text(text, "option", "command", "hint")


def status(value: StatusValue) -> StatusBadge:
    """Wrap a status indicator for semantic rendering."""

    return StatusBadge(value=value)


def join[T](
    render_item: Callable[[T], Message],
    items: Iterable[T],
) -> tuple[Message, ...]:
    """Render and comma-join items."""

    parts: list[Message] = []
    for index, item in enumerate(items):
        if index:
            parts.append(", ")
        parts.append(render_item(item))
    return tuple(parts)


def prefixed_line(
    prefix: Message,
    body: Message | StatusBadge,
    *,
    message_labels: tuple[str, ...] | None = None,
    prefix_labels: tuple[str, ...] | None = None,
) -> PrefixedLine:
    """Build a hanging-indent line without choosing a concrete renderer."""

    return PrefixedLine(
        prefix=prefix,
        body=body,
        message_labels=message_labels,
        prefix_labels=prefix_labels,
    )


def suffixed_line(body: str, suffix: Message) -> SuffixedLine:
    """Append semantic content to a line rendered by `jj log`."""

    return SuffixedLine(body=body, suffix=suffix)


def plain_text(content: Message) -> str:
    """Render semantic template content into plain text."""

    parts: list[str] = []
    _append_plain_text(parts, content)
    return "".join(parts)


def _append_plain_text(parts: list[str], content: Message) -> None:
    if isinstance(content, tuple):
        for item in content:
            _append_plain_text(parts, item)
        return
    if isinstance(content, Template):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            else:
                _append_plain_text(parts, resolve_interpolation(part))
        return
    if isinstance(content, SemanticText):
        parts.append(content.text)
        return
    parts.append(str(content))


def resolve_interpolation(interpolation: Interpolation) -> Message:
    value = interpolation.value
    if isinstance(value, (SemanticText, Template, tuple)):
        return value

    converted = convert(value, interpolation.conversion)
    if interpolation.format_spec:
        return format(converted, interpolation.format_spec)
    return str(converted)
