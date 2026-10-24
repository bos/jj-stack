"""Rich-backed terminal output helpers."""

# Design notes:
#
# - The public API is intentionally just the top-level helper functions and the
#   `configured_console()` context manager. Command modules should not need to manage
#   console objects directly.
#
# - The module keeps stdout and stderr console setup in one place so command
#   modules do not spread Rich policy across the codebase.
#
# - Every object is converted to a Rich renderable before printing, so arbitrary
#   user-facing text does not need per-call Rich escaping.
#
# - Optional time-prefixing lives here alongside the output it affects.
#
# - `--output=jsonl` is a third renderer of the same calls: every helper writes one JSON
#   object per line to stdout instead of styled text, so command modules never branch on it.

from __future__ import annotations

import io
import json
import sys
import threading
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
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

import jj_stack
import jj_stack.ui as ui
from jj_stack.jj.colors import JjColorWhen, SemanticStyles, semantic_styles
from jj_stack.timing import timed

ActionStatus = Literal["applied", "blocked", "planned", "skipped"]

ColorMode = Literal["auto", "always", "never"]
RequestedColorMode = Literal["always", "auto", "debug", "never"]
OutputFormat = Literal["text", "jsonl"]
StyleArg = Style | str
_NO_STYLES = SemanticStyles(())
type ConsoleObject = ui.Renderable | ConsoleRenderable | RichCast


class ProgressLike(Protocol):
    """Minimal progress-handle protocol used by command helpers."""

    def advance(self) -> None: ...


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

    def __rich_console__(self, console, options):
        prefix_options = options.update(width=self.prefix_width, max_width=self.prefix_width)
        body_width = max(1, options.max_width - self.prefix_width)
        body_options = options.update(width=body_width, max_width=body_width)

        prefix_lines = console.render_lines(self.prefix, prefix_options, pad=False)
        prefix_line = prefix_lines[0] if prefix_lines else []
        body_lines = console.render_lines(self.body, body_options, pad=False)

        indent = Segment(" " * self.prefix_width)
        for index, line in enumerate(body_lines):
            if index == 0:
                yield from prefix_line
            else:
                yield indent
            yield from line
            if index < len(body_lines) - 1:
                yield Segment.line()
        yield Segment.line()


@dataclass(slots=True)
class _TrimmedRenderable:
    """Render content with trailing whitespace removed from each line."""

    renderable: RenderableType

    def __rich_console__(self, console, options):
        lines = [
            _rstrip_line_segments(line)
            for line in console.render_lines(self.renderable, options, pad=False)
        ]

        for index, line in enumerate(lines):
            yield from line
            if index < len(lines) - 1:
                yield Segment.line()
        yield Segment.line()


class _ConfiguredConsole:
    """Wrap a Rich console with optional jj-style elapsed-time prefixes."""

    def __init__(
        self,
        console: Console,
        *,
        prefix_style: Style | None,
        start: float | None,
    ) -> None:
        self._console = console
        self._prefix_style = prefix_style
        self._start = start

    def print(
        self,
        *objects: RenderableType,
        end: str = "\n",
        style: Style | str | None = None,
        markup: bool | None = None,
        soft_wrap: bool = False,
    ) -> None:
        if self._start is None:
            self._console.print(
                *objects, end=end, style=style, markup=markup, soft_wrap=soft_wrap
            )
            return

        if not objects:
            objects = (NewLine(),)

        renderables = self._console._collect_renderables(objects, " ", "", markup=markup)
        wrapped = _TimePrefixedRenderable(
            renderable=Group(*renderables),
            end=end,
            prefix_style=self._prefix_style,
            start=self._start,
        )
        self._console.print(wrapped, end="", style=style, soft_wrap=soft_wrap)


class _NullProgress:
    """No-op progress handle returned when live progress is disabled."""

    def advance(self) -> None:
        pass


class _NullSpinner:
    """No-op spinner handle returned when live progress is disabled."""

    def update(self, description: str) -> None:
        del description


class _TextSpinner:
    """Keep changed long-running status visible when stderr is redirected."""

    def __init__(self) -> None:
        self.previous = ""

    def update(self, description: str) -> None:
        if description != self.previous:
            _STDERR_CONSOLE.print(description, markup=False)
            self.previous = description


@dataclass(slots=True)
class _JsonlStatus:
    """Report the current activity as `status` records; `total` adds a count."""

    text: str = ""
    completed: int = 0
    total: int | None = None

    def update(self, description: str) -> None:
        if description != self.text:
            self.text = description
            self.emit()

    def advance(self) -> None:
        self.completed += 1
        self.emit()

    def emit(self) -> None:
        record: dict[str, object] = {"type": "status", "text": self.text}
        if self.total is not None:
            record |= {"completed": self.completed, "total": self.total}
        _write_jsonl(record)


@dataclass(slots=True)
class _RichProgressHandle:
    """Advance one Rich progress task."""

    progress: Progress
    task_id: TaskID

    def advance(self) -> None:
        self.progress.advance(self.task_id)


@dataclass(slots=True)
class _RichSpinnerHandle:
    """Update one Rich status spinner."""

    status: Status

    def update(self, description: str) -> None:
        self.status.update(description)


def _raw_console(stream: IO[str], *, color_mode: ColorMode) -> Console:
    if color_mode == "always":
        return Console(
            file=stream,
            force_terminal=True,
            legacy_windows=False,
            no_color=False,
        )
    if color_mode == "never":
        return Console(file=stream, no_color=True)
    return Console(file=stream)


def _build_console(
    console: Console,
    *,
    semantic_styles: SemanticStyles,
    time_output: bool,
) -> _ConfiguredConsole:
    return _ConfiguredConsole(
        console,
        prefix_style=semantic_styles.for_labels(("prefix", "timestamp")),
        start=jj_stack.PROCESS_START if time_output else None,
    )


def _build_consoles(
    *,
    color_mode: ColorMode = "auto",
    semantic_styles: SemanticStyles = _NO_STYLES,
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
) -> tuple[_ConfiguredConsole, _ConfiguredConsole, SemanticStyles]:
    stdout_console = _raw_console(sys.stdout if stdout is None else stdout, color_mode=color_mode)
    stderr_console = _raw_console(sys.stderr if stderr is None else stderr, color_mode=color_mode)
    if color_mode == "never":
        semantic_styles = _NO_STYLES
    return (
        _build_console(
            stdout_console,
            semantic_styles=semantic_styles,
            time_output=time_output,
        ),
        _build_console(
            stderr_console,
            semantic_styles=semantic_styles,
            time_output=time_output,
        ),
        semantic_styles,
    )


_STDOUT_CONSOLE: _ConfiguredConsole
_STDERR_CONSOLE: _ConfiguredConsole
_SEMANTIC_STYLES: SemanticStyles
_EFFECTIVE_COLOR: RequestedColorMode | None = None
_STDOUT_STREAM: IO[str] = sys.stdout
_STDERR_STREAM: IO[str] = sys.stderr
_TIME_OUTPUT = False
_JSONL = False


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
    color: RequestedColorMode | None,
    semantic_styles: SemanticStyles = _NO_STYLES,
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
    output_format: OutputFormat = "text",
):
    """Temporarily install shared stdout and stderr consoles.

    `color` is the `--color` choice; `adopt_jj_config` fills in jj's own setting and theme once
    command bootstrap has read them. `output_format="jsonl"` sends every helper's output to
    stdout as JSON Lines instead.
    """

    global _STDOUT_CONSOLE
    global _STDERR_CONSOLE
    global _SEMANTIC_STYLES
    global _EFFECTIVE_COLOR
    global _STDOUT_STREAM
    global _STDERR_STREAM
    global _TIME_OUTPUT
    global _JSONL
    previous = (
        _STDOUT_CONSOLE,
        _STDERR_CONSOLE,
        _SEMANTIC_STYLES,
        _EFFECTIVE_COLOR,
        _STDOUT_STREAM,
        _STDERR_STREAM,
        _TIME_OUTPUT,
        _JSONL,
    )
    _STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles(
        color_mode=rich_color_mode(color),
        semantic_styles=semantic_styles,
        stderr=stderr,
        stdout=stdout,
        time_output=time_output,
    )
    _EFFECTIVE_COLOR = color
    _STDOUT_STREAM = sys.stdout if stdout is None else stdout
    _STDERR_STREAM = sys.stderr if stderr is None else stderr
    _TIME_OUTPUT = time_output
    _JSONL = output_format == "jsonl"
    try:
        yield
    finally:
        (
            _STDOUT_CONSOLE,
            _STDERR_CONSOLE,
            _SEMANTIC_STYLES,
            _EFFECTIVE_COLOR,
            _STDOUT_STREAM,
            _STDERR_STREAM,
            _TIME_OUTPUT,
            _JSONL,
        ) = previous


def adopt_jj_config(*, color: str | None, colors: Mapping[str, object]) -> None:
    """Apply jj's `ui.color` and theme to the installed consoles; `--color` keeps precedence."""

    global _STDOUT_CONSOLE
    global _STDERR_CONSOLE
    global _SEMANTIC_STYLES
    global _EFFECTIVE_COLOR
    if _EFFECTIVE_COLOR is None and color in ("always", "auto", "debug", "never"):
        _EFFECTIVE_COLOR = color
    _STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles(
        color_mode=rich_color_mode(_EFFECTIVE_COLOR),
        semantic_styles=semantic_styles(colors),
        stderr=_STDERR_STREAM,
        stdout=_STDOUT_STREAM,
        time_output=_TIME_OUTPUT,
    )


def color_when(*, stdout_is_tty: bool) -> JjColorWhen:
    """Resolve the effective color choice into a `jj --color` value for embedded jj output."""

    if _EFFECTIVE_COLOR in ("always", "debug", "never"):
        return _EFFECTIVE_COLOR
    return "always" if stdout_is_tty else "never"


def semantic_style(*labels: str) -> Style | None:
    """Resolve jj semantic color labels into the active Rich style."""

    return _SEMANTIC_STYLES.for_labels(labels)


def _render_status_badge(status: ui.StatusBadge) -> Text:
    """Render a semantic status badge into Rich text."""

    labels = {
        "ok": ("hint heading",),
        "warn": ("warning heading",),
        "fail": ("error heading",),
        "fixed": ("hint heading",),
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


def style_time_prefix(text: str) -> str:
    """Style the `--time-output` prefix using the active semantic theme."""

    style = semantic_style("prefix", "timestamp")
    if style is None:
        return text
    rich_console = _STDERR_CONSOLE._console
    with rich_console.capture() as capture:
        rich_console.print(Text(text, style=style), end="")
    return capture.get()


def _coerce_renderable(value: ConsoleObject) -> RenderableType:
    # `jj log` output is forwarded verbatim and a change description is untrusted input, so
    # decode every escape introducer rather than only CSI. `Text.append` drops the C0 control
    # that terminates an OSC string but keeps the ESC, which would leave the rest of the report
    # inside an unterminated escape sequence.
    if isinstance(value, str) and "\x1b" in value:
        return Text.from_ansi(value)
    if isinstance(value, ui.StatusBadge):
        return _render_status_badge(value)
    if isinstance(value, ui.PrefixedLine):
        return _render_prefixed_line(value)
    if isinstance(value, ui.SuffixedLine):
        rendered = Text.from_ansi(value.body)
        rendered.append(": ")
        rendered.append_text(rich_text(value.suffix))
        return rendered
    if isinstance(value, ui.DataTable):
        return _render_data_table(value)
    if isinstance(value, str | Template | ui.SemanticText | tuple):
        return rich_text(value)
    return value


def _print(
    kind: str,
    console: _ConfiguredConsole,
    objects: tuple[ConsoleObject, ...],
    *,
    end: str = "\n",
    style: Style | str | None = None,
    soft_wrap: bool,
) -> None:
    renderables = tuple(_coerce_renderable(obj) for obj in objects)
    if not _JSONL:
        console.print(*renderables, end=end, style=style, soft_wrap=soft_wrap)
        return
    # Render as a wide, colorless terminal would, so tables and hanging indents keep their
    # shape while the record carries only text.
    buffer = io.StringIO()
    Console(file=buffer, force_terminal=False, no_color=True, width=10_000).print(
        *renderables, end=end, style=style, soft_wrap=soft_wrap
    )
    _write_jsonl({"type": kind, "text": buffer.getvalue().rstrip("\n")})


def _write_jsonl(record: dict[str, object]) -> None:
    _STDOUT_STREAM.write(json.dumps(record) + "\n")
    _STDOUT_STREAM.flush()


def output(*objects: ConsoleObject, end: str = "\n", soft_wrap: bool = False) -> None:
    """Write plain user-facing output to stdout."""

    _print("output", _STDOUT_CONSOLE, objects, end=end, soft_wrap=soft_wrap)


def machine_output(payload: object) -> None:
    """Write one JSON payload without terminal rendering, or as a JSONL `result` record."""

    if _JSONL:
        _write_jsonl({"type": "result", "data": payload})
        return
    _STDOUT_STREAM.write(json.dumps(payload, indent=2) + "\n")
    _STDOUT_STREAM.flush()


def jsonl_record(record: dict[str, object]) -> None:
    """Write one typed JSONL record for programs; text output shows nothing extra."""

    if _JSONL:
        _write_jsonl(record)


def error(*objects: ConsoleObject, soft_wrap: bool = False) -> None:
    """Write styled error output to stderr."""

    style = semantic_style("error heading") or "red"
    _print("error", _STDERR_CONSOLE, objects, style=style, soft_wrap=soft_wrap)


def stderr_output(*objects: ConsoleObject, soft_wrap: bool = False) -> None:
    """Write plain user-facing output to stderr."""

    _print("output", _STDERR_CONSOLE, objects, soft_wrap=soft_wrap)


def warning(*objects: ConsoleObject, soft_wrap: bool = False) -> None:
    """Write styled warning output to stderr."""

    style = semantic_style("warning heading") or "yellow"
    _print("warning", _STDERR_CONSOLE, objects, style=style, soft_wrap=soft_wrap)


def action_row(*, kind: str | None, status: ActionStatus, body: ui.Message) -> None:
    """Print one action as a status glyph, an optional kind label, and its body."""

    if status == "applied":
        prefix, prefix_style, body_style = "  ✓", ("signature status good",), ()
    elif status == "planned":
        prefix, prefix_style, body_style = "  ~", ("hint heading",), ()
    elif status == "blocked":
        prefix, prefix_style, body_style = "  ✗", ("error heading",), ("warning heading",)
    else:
        prefix, prefix_style, body_style = "  -", ("hint heading",), ()
    message: ui.Message = body if kind is None else (ui.semantic_text(kind, "prefix"), ": ", body)
    output(
        ui.PrefixedLine(
            f"{prefix} ", message, prefix_labels=prefix_style, message_labels=body_style
        )
    )


def note(*objects: ConsoleObject, soft_wrap: bool = False) -> None:
    """Write styled note output to stdout."""

    style = semantic_style("hint heading") or "cyan"
    _print("note", _STDOUT_CONSOLE, objects, style=style, soft_wrap=soft_wrap)


@contextmanager
def _jsonl_status(description: str, *, total: int | None = None) -> Generator[_JsonlStatus]:
    """Report one activity from start to finish; a null `text` marks the finish."""

    handle = _JsonlStatus(total=total)
    handle.update(description)
    try:
        yield handle
    finally:
        _write_jsonl({"type": "status", "text": None})


_LIVE_DISPLAY: Status | Progress | None = None
_LIVE_DISPLAY_LOCK = threading.Lock()
_SUSPENDED_DEPTH = 0


@contextmanager
def _live_display(display: Status | Progress) -> Generator[None]:
    """Track the active live display so a prompting subprocess can hide it."""

    global _LIVE_DISPLAY
    _LIVE_DISPLAY = display
    try:
        yield
    finally:
        _LIVE_DISPLAY = None


@contextmanager
def suspend_live_progress() -> Generator[None]:
    """Hide live progress while a child process may prompt on the terminal.

    ssh and credential helpers write their prompts straight to the terminal, where the next
    spinner redraw would erase them. The display resumes when the outermost caller finishes.
    """

    global _SUSPENDED_DEPTH
    with _LIVE_DISPLAY_LOCK:
        display = _LIVE_DISPLAY
        if display is not None and _SUSPENDED_DEPTH == 0:
            display.stop()
        _SUSPENDED_DEPTH += 1
    try:
        yield
    finally:
        with _LIVE_DISPLAY_LOCK:
            _SUSPENDED_DEPTH -= 1
            if display is not None and _SUSPENDED_DEPTH == 0:
                display.start()


@contextmanager
def spinner(*, description: str, report_changes: bool = False) -> Generator[SpinnerLike]:
    """Render a transient spinner, optionally logging changed status when stderr is redirected."""

    with timed("phase", description):
        if _JSONL:
            with _jsonl_status(description) as handle:
                yield handle
        elif not _stderr_supports_live_progress():
            handle = _TextSpinner() if report_changes else _NullSpinner()
            handle.update(description)
            yield handle
        else:
            with _STDERR_CONSOLE._console.status(description) as status, _live_display(status):
                yield _RichSpinnerHandle(status=status)


@contextmanager
def progress(*, description: str, total: int) -> Generator[ProgressLike]:
    """Render a TTY-only transient progress bar on stderr."""

    if total <= 0:
        yield _NullProgress()
        return
    if _JSONL:
        with _jsonl_status(description, total=total) as handle:
            yield handle
        return
    if not _stderr_supports_live_progress():
        yield _NullProgress()
        return

    with (
        Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=_STDERR_CONSOLE._console,
            transient=True,
        ) as progress_render,
        _live_display(progress_render),
    ):
        task_id = progress_render.add_task(description, total=total)
        yield _RichProgressHandle(progress=progress_render, task_id=task_id)


def _stderr_supports_live_progress() -> bool:
    try:
        return bool(_STDERR_STREAM.isatty())
    except OSError:
        return False


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
        semantic = semantic_style(*content.labels)
        if content.link is not None and _EFFECTIVE_COLOR != "never":
            semantic = _combine_styles(semantic, Style(link=content.link, underline=True))
        rendered.append(
            content.text,
            style=_combine_styles(base_style, semantic),
        )
        return
    if isinstance(content, Text):
        appended = content.copy()
        if base_style is not None and appended.plain:
            appended.stylize(base_style, 0, len(appended.plain))
        rendered.append_text(appended)
        return
    # Interpolated content can be an untrusted change description, and styling here comes from
    # `base_style`, never from escape bytes in the content. Drop every escape introducer rather
    # than leaving one for an ANSI decoder: `Text.from_ansi` consumes only the introducers it
    # recognises and passes `ESC c` (terminal reset) and friends straight through.
    rendered.append(content.replace("\x1b", ""), style=base_style)


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
    message_style = semantic_style(*line.message_labels)
    if isinstance(line.body, ui.StatusBadge):
        message_cell = _coerce_renderable(line.body)
    else:
        message_cell: RenderableType = rich_text(line.body, style=message_style)

    prefix_style = semantic_style(*line.prefix_labels)
    prefix_cell: RenderableType = rich_text(line.prefix, style=prefix_style)
    return _HangingIndentRenderable(
        prefix=prefix_cell,
        prefix_width=prefix_width,
        body=message_cell,
    )


def _render_data_table(table_data: ui.DataTable) -> ConsoleRenderable:
    """Render a lightweight semantic table into a Rich table."""

    box = rich_box.SIMPLE if table_data.box == "simple" else None
    table = Table(
        box=box,
        expand=False,
        header_style="bold",
        pad_edge=False,
        padding=table_data.padding,
        show_edge=False,
        show_header=table_data.show_header,
    )
    for column in table_data.columns:
        table.add_column(column.header, no_wrap=column.no_wrap, width=column.width)
    for row in table_data.rows:
        table.add_row(*(_coerce_renderable(cell) for cell in row))
    return _TrimmedRenderable(table)


def _rstrip_line_segments(line: list[Segment]) -> list[Segment]:
    trimmed = list(line)
    while trimmed and not trimmed[-1].control and not trimmed[-1].text.strip():
        trimmed.pop()
    if trimmed and not trimmed[-1].control:
        last = trimmed[-1]
        trimmed[-1] = Segment(last.text.rstrip(), last.style, last.control)
    return trimmed


# The import-time consoles only serve output emitted before `configured_console` installs
# the ones that carry the invocation's color choice and jj theme.
_STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles()
