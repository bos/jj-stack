"""Semantic message fragments shared across layers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from string.templatelib import Interpolation, Template, convert
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class SemanticText:
    """A short semantic text fragment that should preserve its semantic labels."""

    text: str
    labels: tuple[str, ...]

    def __str__(self) -> str:
        return self.text


StatusValue = Literal["ok", "warn", "fail", "skip"]
type Message = str | Template | SemanticText | tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class StatusBadge:
    """A semantic status indicator rendered by the console layer."""

    value: StatusValue


@dataclass(frozen=True, slots=True)
class PrefixedLine:
    """A hanging-indent line with semantic prefix and body content."""

    prefix: Message | Any
    body: Any
    message_labels: tuple[str, ...] | None = None
    prefix_labels: tuple[str, ...] | None = None


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
    rows: tuple[tuple[Any, ...], ...]
    box: str = "simple"
    expand: bool = False
    header_style: str | None = "bold"
    pad_edge: bool = False
    padding: int | tuple[int, int] | tuple[int, int, int, int] = (0, 0)
    show_edge: bool = False
    show_header: bool = True


def semantic_text(text: str, *labels: str) -> SemanticText:
    """Wrap text with semantic labels for later rendering."""

    return SemanticText(text=text, labels=labels)


def bookmark(name: str) -> SemanticText:
    """Wrap bookmark-like names, including Git remotes, for semantic rendering."""

    label = "remote_bookmarks" if "@" in name else "local_bookmarks"
    return semantic_text(name, label)


def change_id(name: str) -> SemanticText:
    """Wrap a change ID for semantic rendering, shortening it for display."""

    return semantic_text(name[:8], "change_id")


def commit_id(name: str) -> SemanticText:
    """Wrap a commit ID for semantic rendering, shortening it for display."""

    return semantic_text(name[:8], "commit_id")


def revset(text: str) -> SemanticText:
    """Wrap jj revset syntax for semantic rendering."""

    return semantic_text(text, "revset")


def code(text: str) -> SemanticText:
    """Wrap a code-like token for semantic rendering."""

    return semantic_text(text, "code")


def cmd(text: str) -> SemanticText:
    """Wrap a command-line snippet for semantic rendering.

    This can be used for commands, options, or arguments.
    """

    return semantic_text(text, "command", "hint")


def status(value: StatusValue) -> StatusBadge:
    """Wrap a status indicator for semantic rendering."""

    return StatusBadge(value=value)


def join[T](
    render_item: Callable[[T], object],
    items: Iterable[T],
) -> tuple[object, ...]:
    """Render and comma-join items."""

    parts: list[object] = []
    for index, item in enumerate(items):
        if index:
            parts.append(", ")
        parts.append(render_item(item))
    return tuple(parts)


def prefixed_line(
    prefix: Message | Any,
    body: Any,
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


def plain_text(content: Message | Any) -> str:
    """Render semantic template content into plain text."""

    parts: list[str] = []
    _append_plain_text(parts, content)
    return "".join(parts)


def _append_plain_text(parts: list[str], content: Message | Any) -> None:
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


def resolve_interpolation(interpolation: Interpolation) -> Template | SemanticText | Any:
    value = interpolation.value
    if isinstance(value, SemanticText):
        if interpolation.conversion is not None:
            converted = convert(value.text, interpolation.conversion)
            return (
                format(converted, interpolation.format_spec)
                if interpolation.format_spec
                else converted
            )
        if interpolation.format_spec:
            return SemanticText(
                text=format(value.text, interpolation.format_spec),
                labels=value.labels,
            )
        return value
    if isinstance(value, Template):
        if interpolation.conversion is not None or interpolation.format_spec:
            plain = plain_text(value)
            converted = convert(plain, interpolation.conversion)
            if interpolation.format_spec:
                return format(converted, interpolation.format_spec)
            return converted
        return value

    converted = convert(value, interpolation.conversion)
    if interpolation.format_spec:
        return format(converted, interpolation.format_spec)
    return converted
