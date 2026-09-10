from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_stack.errors import CliError
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient
from jj_stack.jj.settings import JjSettings, read_jj_settings


def test_read_jj_settings_lists_everything_once_without_snapshotting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One read serves color, theme, and jj-stack settings, and it never snapshots the repo."""

    observed: list[tuple[str, ...]] = []

    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        observed.append(tuple(command))
        assert Path(kwargs["cwd"]) == tmp_path
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='ui.color = "debug"\ncolors.change_id = "magenta"\njj-stack.labels = ["x"]\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    settings = read_jj_settings(
        cwd=tmp_path,
        cli_args=JjCliArgs(argv=("--config", "jj-stack.logging.level=INFO")),
    )

    assert observed == [
        (
            "jj",
            "--config",
            "jj-stack.logging.level=INFO",
            "--ignore-working-copy",
            "config",
            "list",
            "--include-defaults",
        )
    ]
    assert settings.string("ui", "color") == "debug"
    assert settings.table("colors") == {"change_id": "magenta"}
    assert settings.table("jj-stack") == {"labels": ["x"]}
    assert settings.string("ui", "editor") is None
    assert JjSettings().table("colors") == {}


def test_read_jj_settings_reports_jj_config_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Config error: Invalid config-file path 'missing.toml'\n",
        )

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(CliError, match="Invalid config-file path"):
        read_jj_settings(cwd=tmp_path, cli_args=JjCliArgs())


def test_client_config_strings_come_from_the_listing_unless_the_value_is_not_a_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only string settings short-circuit; anything else still goes to `jj config get`."""

    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout='["code", "--wait"]\n', stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    settings = JjSettings({"git": {"private-commits": "none()"}, "ui": {"editor": ["code"]}})
    client = JjClient(tmp_path, settings=settings)

    assert client.get_config_string("git.private-commits") == "none()"
    assert client.get_config_string("ui.editor") == '["code", "--wait"]'
