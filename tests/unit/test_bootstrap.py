import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import jj_stack.bootstrap
from jj_stack.bootstrap import (
    check_jj_version,
    resolve_repo_root,
)
from jj_stack.errors import CliError


@pytest.fixture(autouse=True)
def _forget_verified_jj_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful checks elsewhere in the process must not mask failures here."""

    monkeypatch.setattr(jj_stack.bootstrap, "_jj_version_verified", False)


def test_check_jj_version_enforces_minimum_version() -> None:
    old_version = subprocess.CompletedProcess(
        args=["jj", "--version"],
        returncode=0,
        stdout="jj 0.47.0\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=old_version):
        with pytest.raises(CliError, match="requires jj 0.47.1 or later"):
            check_jj_version()

    minimum_version = subprocess.CompletedProcess(
        args=["jj", "--version"],
        returncode=0,
        stdout="jj 0.47.1-d16ee1fc88a7\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=minimum_version):
        check_jj_version()


def test_check_jj_version_rejects_unparseable_output() -> None:
    bad_output = subprocess.CompletedProcess(
        args=["jj", "--version"],
        returncode=0,
        stdout="not jj output\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=bad_output):
        with pytest.raises(CliError, match="Could not parse"):
            check_jj_version()


def test_check_jj_version_raises_when_jj_not_installed() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(CliError, match="not installed or is not on PATH"):
            check_jj_version()


def test_check_jj_version_raises_when_version_command_fails() -> None:
    failed = subprocess.CompletedProcess(
        args=["jj", "--version"],
        returncode=1,
        stdout="",
        stderr="some error",
    )
    with patch("subprocess.run", return_value=failed):
        with pytest.raises(CliError, match="failed"):
            check_jj_version()


def test_resolve_repo_root_returns_ancestor_with_jj_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".jj").mkdir(parents=True)
    nested = repo / "src" / "package"
    nested.mkdir(parents=True)

    assert resolve_repo_root(nested) == repo
