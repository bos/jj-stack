from __future__ import annotations

import subprocess
from importlib import import_module
from io import StringIO
from pathlib import Path

import pytest

import jj_stack
import jj_stack.cli as cli_module
import jj_stack.console as console_module
import jj_stack.jj.colors as jj_colors_module
import jj_stack.ui as ui_module
from jj_stack.jj.cli_args import JjCliArgs


def _style_cls():
    return import_module("rich.style").Style


def test_cli_color_config_read_ignores_working_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(command, **kwargs):
        assert command == [
            "jj",
            "--ignore-working-copy",
            "config",
            "get",
            "ui.color",
        ]
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(command, 0, stdout="debug\n", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert (
        cli_module._load_configured_jj_color(
            repo=tmp_path,
            cli_args=JjCliArgs(),
        )
        == "debug"
    )


def test_time_output_prefix_uses_prefix_and_timestamp_semantic_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path.cwd()
    stdout = 'colors.prefix.bold\0true\ncolors.timestamp\0"cyan"\n'

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(jj_colors_module.subprocess, "run", fake_run)
    monkeypatch.setattr(jj_stack, "PROCESS_START", 10.0)
    monkeypatch.setattr(console_module.time, "perf_counter", lambda: 12.5)

    output = StringIO()
    with console_module.configured_console(
        stdout=output,
        stderr=StringIO(),
        color_mode="always",
        repo=repo,
        time_output=True,
    ):
        console_module.output("timed")

    assert output.getvalue() == "\x1b[1;36m[2.500000] \x1b[0mtimed\n"


def test_machine_output_bypasses_terminal_formatting() -> None:
    output = StringIO()
    payload = '{"url":"https://example.test/' + ("long-path/" * 20) + '"}'

    with console_module.configured_console(
        stdout=output,
        stderr=StringIO(),
        color_mode="always",
        time_output=True,
    ):
        console_module.machine_output(payload)

    assert output.getvalue() == f"{payload}\n"


def test_semantic_style_uses_machine_readable_jj_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path.cwd()
    stdout = (
        'colors.change_id\0"ansi-color-81"\n'
        "colors.working_copy.bold\0true\n"
        'colors."working_copy change_id"\0"bright magenta"\n'
    )

    def fake_run(command, **kwargs):
        assert command[:4] == ["jj", "--ignore-working-copy", "config", "list"]
        assert "colors" in command
        assert kwargs["cwd"] == repo
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(jj_colors_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="always",
        repo=repo,
    ):
        assert console_module.semantic_style("missing") is None
        assert console_module.semantic_style("change_id") == _style_cls()(color="color(81)")
        assert console_module.semantic_style("working_copy", "change_id") == _style_cls()(
            color="bright_magenta",
            bold=True,
        )


def test_rich_text_renders_template_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path.cwd()
    stdout = (
        'colors.local_bookmarks\0"green"\n'
        "colors.change_id.bold\0true\n"
        'colors.change_id\0"ansi-color-81"\n'
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(jj_colors_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="always",
        repo=repo,
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


def test_joined_semantic_template_interpolation_renders_plain_text_and_styles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path.cwd()
    stdout = 'colors.local_bookmarks\0"green"\n'
    first = "jj-stack/fix-one-aaaaaaaa"
    second = "jj-stack/fix-two-bbbbbbbb"
    expected = f"matches: {first}, {second}."

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(jj_colors_module.subprocess, "run", fake_run)

    bookmarks = ui_module.join(ui_module.bookmark, (first, second))
    message = t"matches: {bookmarks}."

    assert ui_module.plain_text(message) == expected

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="always",
        repo=repo,
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


def test_revset_uses_semantic_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path.cwd()
    stdout = 'colors.revset\0"blue"\n'

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(jj_colors_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="always",
        repo=repo,
    ):
        text = console_module.rich_text(ui_module.revset("trunk()"))

    assert text.plain == "trunk()"
    assert text.spans == [import_module("rich.text").Span(0, 7, _style_cls()(color="blue"))]
