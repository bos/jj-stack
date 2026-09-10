from __future__ import annotations

import tomllib
from importlib import import_module
from io import StringIO

import jj_stack.console as console_module
import jj_stack.ui as ui_module
from jj_stack.jj.colors import SemanticStyles, semantic_styles


def _style_cls():
    return import_module("rich.style").Style


def _theme(listing: str) -> SemanticStyles | None:
    """Build the jj theme from lines shaped like `jj config list colors` output."""

    return semantic_styles(tomllib.loads(listing).get("colors", {}))


def test_color_when_prefers_the_flag_then_jj_config_then_the_terminal() -> None:
    with console_module.configured_console(color="never"):
        console_module.adopt_jj_config(color="always", colors={})
        assert console_module.color_when(stdout_is_tty=True) == "never"
    with console_module.configured_console(color=None):
        console_module.adopt_jj_config(color="debug", colors={})
        assert console_module.color_when(stdout_is_tty=False) == "debug"
    with console_module.configured_console(color=None):
        console_module.adopt_jj_config(color="rainbow", colors={})
        assert console_module.color_when(stdout_is_tty=True) == "always"
        assert console_module.color_when(stdout_is_tty=False) == "never"


def test_machine_output_bypasses_terminal_formatting() -> None:
    output = StringIO()
    payload = '{"url":"https://example.test/' + ("long-path/" * 20) + '"}'

    with console_module.configured_console(
        stdout=output,
        stderr=StringIO(),
        color="always",
        time_output=True,
    ):
        console_module.machine_output(payload)

    assert output.getvalue() == f"{payload}\n"


def test_output_neutralizes_terminal_escapes_from_change_descriptions() -> None:
    """No change description can carry an escape introducer to the terminal."""

    coloured = "\x1b[1;36mcoloured\x1b[0m"

    def render(*objects, color: console_module.RequestedColorMode = "never") -> str:
        output = StringIO()
        with console_module.configured_console(
            stdout=output,
            stderr=StringIO(),
            color=color,
        ):
            console_module.output(*objects, soft_wrap=True)
        return output.getvalue()

    raw = render("osc \x1b]0;PWNED\x07 tail")
    assert "\x1b" not in raw and "\x07" not in raw
    assert raw.startswith("osc ") and raw.endswith(" tail\n")
    assert render(coloured) == "coloured\n"

    styled = render(coloured, color="always")
    assert "coloured" in styled and "\x1b[" in styled
    suffixed = ui_module.suffixed_line(coloured, "not submitted")
    suffixed_output = render(suffixed, color="always")
    assert "coloured" in suffixed_output
    assert "not submitted" in suffixed_output
    assert "\x1b[" in suffixed_output


def test_semantic_style_uses_jj_color_config() -> None:
    theme = _theme(
        'colors.change_id = "ansi-color-81"\n'
        "colors.working_copy.bold = true\n"
        'colors."working_copy change_id" = "bright magenta"\n'
    )

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color="always",
        semantic_styles=theme,
    ):
        assert console_module.semantic_style("missing") is None
        assert console_module.semantic_style("change_id") == _style_cls()(color="color(81)")
        assert console_module.semantic_style("working_copy", "change_id") == _style_cls()(
            color="bright_magenta",
            bold=True,
        )


def test_rich_text_renders_template_semantics() -> None:
    theme = _theme(
        'colors.local_bookmarks = "green"\n'
        "colors.change_id.bold = true\n"
        'colors.change_id.fg = "ansi-color-81"\n'
    )

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color="always",
        semantic_styles=theme,
    ):
        text = console_module.rich_text(
            t"delete {ui_module.bookmark('jj-stack/feature-aaaaaaaa')} for "
            t"{ui_module.change_id('aaaa1111bbbb2222')}"
        )

    assert text.plain == "delete jj-stack/feature-aaaaaaaa for aaaa1111"
    assert text.spans[0].start == 7
    assert text.spans[0].end == 32
    assert text.spans[0].style == _style_cls()(color="green")
    assert text.spans[1].start == 37
    assert text.spans[1].end == 45
    assert text.spans[1].style == _style_cls()(color="color(81)", bold=True)


def test_joined_semantic_template_interpolation_renders_plain_text_and_styles() -> None:
    theme = _theme('colors.local_bookmarks = "green"\n')
    first = "jj-stack/fix-one-aaaaaaaa"
    second = "jj-stack/fix-two-bbbbbbbb"
    expected = f"matches: {first}, {second}."

    bookmarks = ui_module.join(ui_module.bookmark, (first, second))
    message = t"matches: {bookmarks}."

    assert ui_module.plain_text(message) == expected

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color="always",
        semantic_styles=theme,
    ):
        text = console_module.rich_text(message)

    style = _style_cls()(color="green")
    span_cls = import_module("rich.text").Span
    first_start = expected.index(first)
    second_start = expected.index(second)
    assert text.plain == expected
    assert text.spans == [
        span_cls(first_start, first_start + len(first), style),
        span_cls(second_start, second_start + len(second), style),
    ]
