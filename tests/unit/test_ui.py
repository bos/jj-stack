from __future__ import annotations

import subprocess
from importlib import import_module
from io import StringIO
from pathlib import Path

import pytest

from jj_review import console as console_module, ui as ui_module


def _style_cls():
    return import_module("rich.style").Style


def test_time_output_prefix_uses_prefix_and_timestamp_semantic_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = 'colors.prefix.bold\0true\ncolors.timestamp\0"cyan"\n'

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)
    monkeypatch.setattr(console_module.time, "perf_counter", lambda: 0.0)

    console_cls = import_module("rich.console").Console
    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="always",
        repository=repository,
        time_output=True,
    ):
        console = console_cls(width=40)
        lines = console.render_lines(
            console_module._TimePrefixedRenderable(
                renderable="timed",
                end="",
                prefix_style=console_module.semantic_style("prefix", "timestamp"),
                start=0.0,
            ),
            console.options,
            pad=False,
        )

    prefix_segment = lines[0][0]
    assert prefix_segment.text == "[0.000000] "
    assert prefix_segment.style == _style_cls()(color="cyan", bold=True)


def test_semantic_style_uses_machine_readable_jj_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = (
        'colors.change_id\0"ansi-color-81"\n'
        "colors.working_copy.bold\0true\n"
        'colors."working_copy change_id"\0"bright magenta"\n'
    )

    def fake_run(command, **kwargs):
        assert command == [
            "jj",
            "config",
            "list",
            "--include-defaults",
            "colors",
            "-T",
            r'name ++ "\0" ++ json(value) ++ "\n"',
        ]
        assert kwargs["cwd"] == repository
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
        repository=repository,
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
    repository = Path.cwd()
    stdout = (
        'colors.local_bookmarks\0"green"\n'
        "colors.change_id.bold\0true\n"
        'colors.change_id\0"ansi-color-81"\n'
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
        repository=repository,
    ):
        text = console_module.rich_text(
            t"delete {ui_module.bookmark('review/feature-aaaaaaaa')} for "
            t"{ui_module.change_id('aaaa1111bbbb2222')}"
        )

    assert text.plain == "delete review/feature-aaaaaaaa for aaaa1111"
    assert text.spans[0].start == 7
    assert text.spans[0].end == 30
    assert text.spans[0].style == _style_cls()(color="green")
    assert text.spans[1].start == 35
    assert text.spans[1].end == 43
    assert text.spans[1].style == _style_cls()(color="color(81)", bold=True)


def test_revset_uses_semantic_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = 'colors.revset\0"blue"\n'

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
        repository=repository,
    ):
        text = console_module.rich_text(ui_module.revset("trunk()"))

    assert text.plain == "trunk()"
    assert text.spans == [import_module("rich.text").Span(0, 7, _style_cls()(color="blue"))]


