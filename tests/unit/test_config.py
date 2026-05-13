from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_review.config import load_config, parse_jj_review_config_toml
from jj_review.errors import CliError
from jj_review.jj.client import JjCliArgs, JjClient


def _patch_config_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
) -> None:
    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert command[0] == "jj"
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        assert Path(kwargs["cwd"]) == tmp_path
        assert kwargs["text"] is True
        assert tuple(command[-3:]) == ("config", "list", "jj-review")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)


def test_load_config_returns_defaults_when_no_keys_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config_output(monkeypatch, tmp_path, "")
    config = load_config(jj_client=JjClient(tmp_path))

    assert config.logging.level == "WARNING"
    assert config.bookmark_prefix == "review"
    assert config.cleanup_user_bookmarks is False
    assert config.labels == []


def test_load_config_parses_resolved_jj_review_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = "\n".join(
        [
            'jj-review.bookmark_prefix = "bosullivan"',
            "jj-review.cleanup_user_bookmarks = true",
            'jj-review.reviewers = ["octocat"]',
            'jj-review.team_reviewers = ["platform"]',
            'jj-review.use_bookmarks = ["potato/*", "", "spam/eggs", "potato/*"]',
            'jj-review.labels = ["needs-review"]',
            'jj-review.logging.level = "info"',
            "",
        ]
    )
    _patch_config_output(monkeypatch, tmp_path, stdout)
    config = load_config(jj_client=JjClient(tmp_path))

    assert config.logging.level == "INFO"
    assert config.bookmark_prefix == "bosullivan"
    assert config.cleanup_user_bookmarks is True
    assert config.reviewers == ["octocat"]
    assert config.team_reviewers == ["platform"]
    assert config.labels == ["needs-review"]
    assert config.use_bookmarks == ["potato/*", "spam/eggs"]


def test_load_config_ignores_unknown_keys_inside_jj_review_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = 'jj-review.potato = "round"\n'

    _patch_config_output(monkeypatch, tmp_path, stdout)
    config = load_config(jj_client=JjClient(tmp_path))

    assert config.bookmark_prefix == "review"
    assert config.labels == []


def test_load_config_rejects_likely_top_level_typo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = 'jj-review.bookmark_prefx = "bos"\n'
    _patch_config_output(monkeypatch, tmp_path, stdout)

    with pytest.raises(CliError, match=r"Did you mean \[jj-review\]\.bookmark_prefix\?"):
        load_config(jj_client=JjClient(tmp_path))


def test_load_config_rejects_invalid_logging_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = 'jj-review.logging.level = "DEBIG"\n'
    _patch_config_output(monkeypatch, tmp_path, stdout)

    with pytest.raises(CliError, match="Invalid logging level"):
        load_config(jj_client=JjClient(tmp_path))


def test_load_config_rejects_bookmark_prefix_with_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = 'jj-review.bookmark_prefix = "bosullivan/review"\n'
    _patch_config_output(monkeypatch, tmp_path, stdout)

    with pytest.raises(CliError, match="bookmark_prefix"):
        load_config(jj_client=JjClient(tmp_path))


def test_parse_jj_review_config_toml_extracts_nested_tables() -> None:
    stdout = "\n".join(
        [
            'jj-review.bookmark_prefix = "bos"',
            'jj-review.logging.level = "INFO"',
            "",
        ]
    )
    parsed = parse_jj_review_config_toml(stdout)
    assert parsed == {"bookmark_prefix": "bos", "logging": {"level": "INFO"}}


def test_load_config_wraps_jj_command_failure_with_user_facing_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == tmp_path
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Config error: Invalid config-file path 'missing.toml'\n",
        )

    monkeypatch.setattr(subprocess, "run", run)
    client = JjClient(tmp_path)

    with pytest.raises(CliError) as exc_info:
        load_config(jj_client=client)

    message = str(exc_info.value)
    assert message.startswith("Could not load jj-review config:")
    assert "Invalid config-file path" in message


def test_load_config_surfaces_cli_args_through_to_jj(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == tmp_path
        observed_commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='jj-review.bookmark_prefix = "bos"\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    client = JjClient(
        tmp_path,
        cli_args=JjCliArgs(argv=("--config", "jj-review.bookmark_prefix=bos")),
    )
    config = load_config(jj_client=client)

    assert config.bookmark_prefix == "bos"
    assert observed_commands == [
        (
            "jj",
            "--config",
            "jj-review.bookmark_prefix=bos",
            "config",
            "list",
            "jj-review",
        )
    ]
