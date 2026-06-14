"""Rich-backed terminal output helpers."""

# Design notes:
#
# - The public API is intentionally just the top-level helper functions and the
#   `configured_console()` context manager. Command modules should not need to manage
#   console objects directly.
#
# - The module keeps stdout and stderr console setup in one place so we can
#   migrate commands from `print(...)` incrementally without spreading Rich
#   policy across the codebase.
#
# - `markup=False` remains the default so arbitrary user-facing text does not
#   need per-call Rich escaping.
#
# - Optional time-prefixing stays here even though most commands still use the
#   legacy `print` shim today. We are likely to need the same behavior again as
#   command output moves onto these helpers.

from __future__ import annotations

import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from string.templatelib import Template
from typing import IO, Literal, Protocol

from rich import box as rich_box
from rich.console import Console, ConsoleRenderable, Group, NewLine, RenderableType, RichCast
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)
from rich.segment import Segment
from rich.status import Status
from rich.style import Style
from rich.table import Table
from rich.text import Text

import jj_stack.ui as ui
from jj_stack.jj.client import JjCliArgs
from jj_stack.jj.colors import SemanticStyles, load_semantic_styles

_NO_CLI_ARGS = JjCliArgs()

SIMPLE = rich_box.SIMPLE

ColorMode = Literal["auto", "always", "never"]
RequestedColorMode = Literal["always", "auto", "debug", "never"]
StyleArg = Style | str
type ConsoleObject = (
    ui.Renderable | ConsoleRenderable | RichCast
)


class ProgressLike(Protocol):
    """Minimal progress-handle protocol used by command helpers."""

    def advance(self, amount: int = 1) -> None: ...


class SpinnerLike(Protocol):
    """Minimal spinner-handle protocol used by command helpers."""

    def update(self, description: str) -> None: ...


@dataclass(slots=True)
class _TimePrefixedRenderable:
    """Render content with a Rich-managed elapsed-time prefix on every line."""

    renderable: RenderableType
    end: str
    prefix_style: Style | None
    start: float

    def __rich_console__(self, console, options):
        prefix = f"[{time.perf_counter() - self.start:0.6f}] "
        prefix_width = len(prefix)
        inner_width = max(1, options.max_width - prefix_width)
        inner_options = options.update(width=inner_width, max_width=inner_width)
        lines = console.render_lines(self.renderable, inner_options, pad=False)
        prefix_segment = Segment(prefix, self.prefix_style)
        for index, line in enumerate(lines):
            yield prefix_segment
            yield from line
            if index < len(lines) - 1:
                yield Segment.line()
        if self.end == "\n":
            yield Segment.line()
        elif self.end:
            yield from console.render(self.end, options)


@dataclass(slots=True)
class _HangingIndentRenderable:
    """Render a prefix once and indent wrapped body lines to the same column."""

    prefix: RenderableType
    prefix_width: int
    body: RenderableType
    end: str = "\n"

    def __rich_console__(self, console, options):
        if options.no_wrap:
            no_wrap_options = options.update(no_wrap=True, overflow="ignore")
            yield from console.render(self.prefix, no_wrap_options)
            yield from console.render(self.body, no_wrap_options)
            return

        prefix_options = options.update(width=self.prefix_width, max_width=self.prefix_width)
        body_width = max(1, options.max_width - self.prefix_width)
        body_options = options.update(width=body_width, max_width=body_width)

        prefix_lines = console.render_lines(self.prefix, prefix_options, pad=False)
        prefix_line = prefix_lines[0] if prefix_lines else []
        body_lines = console.render_lines(self.body, body_options, pad=False)
        if not body_lines:
            body_lines = [[]]

        indent = Segment(" " * self.prefix_width)
        for index, line in enumerate(body_lines):
            if index == 0:
                yield from prefix_line
            else:
                yield indent
            yield from line
            if index < len(body_lines) - 1:
                yield Segment.line()
        if self.end == "\n":
            yield Segment.line()
        elif self.end:
            yield from console.render(self.end, options)


@dataclass(slots=True)
class _TrimmedRenderable:
    """Render content with trailing whitespace removed from each line."""

    renderable: RenderableType
    end: str = "\n"

    def __rich_console__(self, console, options):
        lines = [
            _rstrip_line_segments(line)
            for line in console.render_lines(self.renderable, options, pad=False)
        ]
        while lines and not _line_text(lines[0]):
            lines.pop(0)
        while lines and not _line_text(lines[-1]):
            lines.pop()

        for index, line in enumerate(lines):
            yield from line
            if index < len(lines) - 1:
                yield Segment.line()
        if self.end == "\n":
            yield Segment.line()
        elif self.end:
            yield from console.render(self.end, options)


class _ConfiguredConsole:
    """Wrap a Rich console with optional jj-style elapsed-time prefixes."""

    def __init__(
        self,
        console: Console,
        *,
        prefix_style: Style | None,
        start: float | None,
        time_output: bool,
    ) -> None:
        self._console = console
        self._prefix_style = prefix_style
        self._start = start
        self._time_output = time_output

    def print(
        self,
        *objects,
        sep: str = " ",
        end: str = "\n",
        style=None,
        justify=None,
        overflow=None,
        no_wrap=None,
        emoji=None,
        markup=None,
        highlight=None,
        width=None,
        height=None,
        crop: bool = True,
        soft_wrap=None,
        new_line_start: bool = False,
    ) -> None:
        if not self._time_output or self._start is None:
            self._console.print(
                *objects,
                sep=sep,
                end=end,
                style=style,
                justify=justify,
                overflow=overflow,
                no_wrap=no_wrap,
                emoji=emoji,
                markup=markup,
                highlight=highlight,
                width=width,
                height=height,
                crop=crop,
                soft_wrap=soft_wrap,
                new_line_start=new_line_start,
            )
            return

        if not objects:
            objects = (NewLine(),)

        renderables = self._console._collect_renderables(
            objects,
            sep,
            "",
            justify=justify,
            emoji=emoji,
            markup=markup,
            highlight=highlight,
        )
        wrapped = _TimePrefixedRenderable(
            renderable=Group(*renderables),
            end=end,
            prefix_style=self._prefix_style,
            start=self._start,
        )
        self._console.print(
            wrapped,
            end="",
            style=style,
            justify=justify,
            overflow=overflow,
            no_wrap=no_wrap,
            width=width,
            height=height,
            crop=crop,
            soft_wrap=soft_wrap,
            new_line_start=new_line_start,
        )


class _NullProgress:
    """No-op progress handle returned when live progress is disabled."""

    def advance(self, amount: int = 1) -> None:
        del amount


class _NullSpinner:
    """No-op spinner handle returned when live progress is disabled."""

    def update(self, description: str) -> None:
        del description


@dataclass(slots=True)
class _RichProgressHandle:
    """Advance one Rich progress task."""

    progress: Progress
    task_id: TaskID

    def advance(self, amount: int = 1) -> None:
        self.progress.advance(self.task_id, amount)


@dataclass(slots=True)
class _RichSpinnerHandle:
    """Update one Rich status spinner."""

    status: Status

    def update(self, description: str) -> None:
        self.status.update(description)


def _build_console(
    stream: IO[str],
    *,
    color_mode: ColorMode,
    semantic_styles: SemanticStyles | None,
    time_output: bool,
    start: float | None,
) -> _ConfiguredConsole:
    if color_mode == "always":
        console = Console(file=stream, force_terminal=True)
    elif color_mode == "never":
        console = Console(file=stream, no_color=True)
    else:
        console = Console(file=stream)
    return _ConfiguredConsole(
        console,
        prefix_style=(
            None
            if semantic_styles is None
            else semantic_styles.for_labels(("prefix", "timestamp"))
        ),
        start=start,
        time_output=time_output,
    )


def _build_consoles(
    *,
    cli_args: JjCliArgs,
    color_mode: ColorMode = "auto",
    repository: Path | None = None,
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
) -> tuple[_ConfiguredConsole, _ConfiguredConsole, SemanticStyles | None]:
    start = time.perf_counter() if time_output else None
    stdout_stream = sys.stdout if stdout is None else stdout
    stderr_stream = sys.stderr if stderr is None else stderr
    semantic_styles = load_semantic_styles(repository=repository, cli_args=cli_args)
    return (
        _build_console(
            stdout_stream,
            color_mode=color_mode,
            semantic_styles=semantic_styles,
            time_output=time_output,
            start=start,
        ),
        _build_console(
            stderr_stream,
            color_mode=color_mode,
            semantic_styles=semantic_styles,
            time_output=time_output,
            start=start,
        ),
        semantic_styles,
    )


_STDOUT_CONSOLE: _ConfiguredConsole
_STDERR_CONSOLE: _ConfiguredConsole
_SEMANTIC_STYLES: SemanticStyles | None
_REQUESTED_COLOR_MODE: RequestedColorMode | None = None
_ACTIVE_COLOR_MODE: ColorMode = "auto"
_STDERR_STREAM: IO[str] = sys.stderr


def rich_color_mode(color_mode: RequestedColorMode | None) -> ColorMode:
    """Map `jj`-style color modes onto Rich's supported console modes."""

    if color_mode in {"always", "debug"}:
        return "always"
    if color_mode == "never":
        return "never"
    return "auto"


@contextmanager
def configured_console(
    *,
    cli_args: JjCliArgs = _NO_CLI_ARGS,
    color_mode: ColorMode = "auto",
    repository: Path | None = None,
    requested_color_mode: RequestedColorMode | None = None,
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
):
    """Temporarily install shared stdout and stderr consoles."""

    global _STDOUT_CONSOLE
    global _STDERR_CONSOLE
    global _SEMANTIC_STYLES
    global _REQUESTED_COLOR_MODE
    global _ACTIVE_COLOR_MODE
    global _STDERR_STREAM
    previous_stdout = _STDOUT_CONSOLE
    previous_stderr = _STDERR_CONSOLE
    previous_semantic_styles = _SEMANTIC_STYLES
    previous_requested_color_mode = _REQUESTED_COLOR_MODE
    previous_active_color_mode = _ACTIVE_COLOR_MODE
    previous_stderr_stream = _STDERR_STREAM
    _STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles(
        cli_args=cli_args,
        color_mode=color_mode,
        repository=repository,
        stderr=stderr,
        stdout=stdout,
        time_output=time_output,
    )
    _REQUESTED_COLOR_MODE = requested_color_mode
    _ACTIVE_COLOR_MODE = color_mode
    _STDERR_STREAM = sys.stderr if stderr is None else stderr
    try:
        yield
    finally:
        _STDOUT_CONSOLE = previous_stdout
        _STDERR_CONSOLE = previous_stderr
        _SEMANTIC_STYLES = previous_semantic_styles
        _REQUESTED_COLOR_MODE = previous_requested_color_mode
        _ACTIVE_COLOR_MODE = previous_active_color_mode
        _STDERR_STREAM = previous_stderr_stream


def requested_color_mode() -> RequestedColorMode | None:
    """Return the active CLI `--color` override, if one was supplied."""

    return _REQUESTED_COLOR_MODE


def semantic_style(*labels: str) -> Style | None:
    """Resolve jj semantic color labels into the active Rich style."""

    if _SEMANTIC_STYLES is None:
        return None
    return _SEMANTIC_STYLES.for_labels(labels)


def _render_status_badge(status: ui.StatusBadge) -> Text:
    """Render a semantic status badge into Rich text."""

    labels = {
        "ok": ("hint heading",),
        "warn": ("warning heading",),
        "fail": ("error heading",),
        "skip": ("hint heading",),
    }
    return rich_text(ui.semantic_text(status.value, *labels[status.value]))


def rich_text(
    content: ui.Message,
    *,
    style: StyleArg | None = None,
) -> Text:
    """Render semantic template content into Rich `Text`."""

    rendered = Text("") if style is None else Text("", style=style)
    _append_rich_text(rendered, content, base_style=style)
    return rendered


def ansi_text(text: str) -> Text:
    """Decode ANSI-styled text into a Rich `Text` renderable."""

    return Text.from_ansi(text)


def style_time_prefix(text: str) -> str:
    """Style the `--time-output` prefix using the active semantic theme."""

    style = semantic_style("prefix", "timestamp")
    if style is None:
        return text
    if not isinstance(_STDERR_CONSOLE, _ConfiguredConsole):
        return text
    rich_console = _STDERR_CONSOLE._console
    with rich_console.capture() as capture:
        rich_console.print(Text(text, style=style), end="")
    return capture.get()


def _coerce_renderable(value: ConsoleObject) -> RenderableType:
    if isinstance(value, str) and "\x1b[" in value:
        return ansi_text(value)
    if isinstance(value, ui.StatusBadge):
        return _render_status_badge(value)
    if isinstance(value, ui.PrefixedLine):
        return _render_prefixed_line(value)
    if isinstance(value, ui.DataTable):
        return _render_data_table(value)
    if isinstance(value, str | Template | ui.SemanticText | tuple):
        return rich_text(value)
    if isinstance(value, str | ConsoleRenderable | RichCast):
        return value
    return str(value)


def output(*objects: ConsoleObject, **kwargs) -> None:
    """Write plain user-facing output to stdout."""

    kwargs.setdefault("markup", False)
    _STDOUT_CONSOLE.print(*(_coerce_renderable(obj) for obj in objects), **kwargs)


def error(*objects: ConsoleObject, **kwargs) -> None:
    """Write styled error output to stderr."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", semantic_style("error heading") or "red")
    _STDERR_CONSOLE.print(*(_coerce_renderable(obj) for obj in objects), **kwargs)


def stderr_output(*objects: ConsoleObject, **kwargs) -> None:
    """Write plain user-facing output to stderr."""

    kwargs.setdefault("markup", False)
    _STDERR_CONSOLE.print(*(_coerce_renderable(obj) for obj in objects), **kwargs)


def warning(*objects: ConsoleObject, **kwargs) -> None:
    """Write styled warning output to stderr."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", semantic_style("warning heading") or "yellow")
    _STDERR_CONSOLE.print(*(_coerce_renderable(obj) for obj in objects), **kwargs)


def note(*objects: ConsoleObject, **kwargs) -> None:
    """Write styled note output to stdout."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", semantic_style("hint heading") or "cyan")
    _STDOUT_CONSOLE.print(*(_coerce_renderable(obj) for obj in objects), **kwargs)


@contextmanager
def spinner(*, description: str) -> Generator[SpinnerLike]:
    """Render a TTY-only transient spinner on stderr."""

    if not _stream_supports_live_progress(_STDERR_STREAM):
        yield _NullSpinner()
        return

    progress_console = _progress_console(stream=_STDERR_STREAM, color_mode=_ACTIVE_COLOR_MODE)
    with progress_console.status(description) as status:
        yield _RichSpinnerHandle(status=status)


@contextmanager
def progress(*, description: str, total: int) -> Generator[ProgressLike]:
    """Render a TTY-only transient progress bar on stderr."""

    if total <= 0 or not _stream_supports_live_progress(_STDERR_STREAM):
        yield _NullProgress()
        return

    progress_console = _progress_console(stream=_STDERR_STREAM, color_mode=_ACTIVE_COLOR_MODE)
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=progress_console,
        transient=True,
    ) as progress_render:
        task_id = progress_render.add_task(description, total=total)
        yield _RichProgressHandle(progress=progress_render, task_id=task_id)


def _stream_supports_live_progress(stream: IO[str]) -> bool:
    try:
        return bool(stream.isatty())
    except OSError:
        return False


def _progress_console(*, stream: IO[str], color_mode: ColorMode) -> Console:
    if color_mode == "always":
        return Console(file=stream, force_terminal=True)
    if color_mode == "never":
        return Console(file=stream, no_color=True)
    return Console(file=stream)


def _append_rich_text(
    rendered: Text,
    content: ui.Message,
    *,
    base_style: StyleArg | None,
) -> None:
    if isinstance(content, tuple):
        for item in content:
            _append_rich_text(rendered, item, base_style=base_style)
        return
    if isinstance(content, Template):
        for part in content:
            if isinstance(part, str):
                rendered.append(part, style=base_style)
            else:
                _append_rich_text(
                    rendered,
                    ui.resolve_interpolation(part),
                    base_style=base_style,
                )
        return
    if isinstance(content, ui.SemanticText):
        rendered.append(
            content.text,
            style=_combine_styles(base_style, semantic_style(*content.labels)),
        )
        return
    if isinstance(content, Text):
        appended = content.copy()
        if base_style is not None and appended.plain:
            appended.stylize(base_style, 0, len(appended.plain))
        rendered.append_text(appended)
        return
    rendered.append(content, style=base_style)


def _combine_styles(
    base_style: StyleArg | None,
    extra_style: StyleArg | None,
) -> StyleArg | None:
    if base_style is None:
        return extra_style
    if extra_style is None:
        return base_style
    return _to_rich_style(base_style) + _to_rich_style(extra_style)


def _to_rich_style(style: StyleArg) -> Style:
    if isinstance(style, Style):
        return style
    return Style.parse(style)


def _render_prefixed_line(line: ui.PrefixedLine) -> _HangingIndentRenderable:
    """Render one semantic hanging-indent line."""

    prefix_width = max(1, len(ui.plain_text(line.prefix)))
    message_style = (
        semantic_style(*line.message_labels) if line.message_labels is not None else None
    )
    if isinstance(line.body, ui.StatusBadge):
        message_cell = _coerce_renderable(line.body)
    else:
        message_cell: RenderableType = rich_text(line.body, style=message_style)

    prefix_style = (
        semantic_style(*line.prefix_labels) if line.prefix_labels is not None else None
    )
    prefix_cell: RenderableType = rich_text(line.prefix, style=prefix_style)
    return _HangingIndentRenderable(
        prefix=prefix_cell,
        prefix_width=prefix_width,
        body=message_cell,
    )


def _render_data_table(table_data: ui.DataTable) -> ConsoleRenderable:
    """Render a lightweight semantic table into a Rich table."""

    box = SIMPLE if table_data.box == "simple" else None
    table = Table(
        box=box,
        expand=table_data.expand,
        header_style=table_data.header_style,
        pad_edge=table_data.pad_edge,
        padding=table_data.padding,
        show_edge=table_data.show_edge,
        show_header=table_data.show_header,
    )
    for column in table_data.columns:
        table.add_column(column.header, no_wrap=column.no_wrap, width=column.width)
    for row in table_data.rows:
        table.add_row(*(_coerce_renderable(cell) for cell in row))
    return _TrimmedRenderable(table)


def _line_text(line: list[Segment]) -> str:
    return "".join(segment.text for segment in line if not segment.control)


def _rstrip_line_segments(line: list[Segment]) -> list[Segment]:
    trimmed = list(line)
    while trimmed and not trimmed[-1].control and not trimmed[-1].text.strip():
        trimmed.pop()
    if not trimmed:
        return []

    last = trimmed[-1]
    if not last.control:
        right_trimmed = last.text.rstrip()
        if right_trimmed != last.text:
            if right_trimmed:
                trimmed[-1] = Segment(right_trimmed, last.style, last.control)
            else:
                trimmed.pop()
    return trimmed


_STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles(cli_args=_NO_CLI_ARGS)
