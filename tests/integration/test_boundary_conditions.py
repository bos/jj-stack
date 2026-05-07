from __future__ import annotations

from pathlib import Path

import pytest

from ..support.integration_helpers import (
    commit_file,
    init_repo,
    run_command,
)
from ..support.output_assertions import assert_output_contains
from .submit_command_helpers import run_main


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("status", ()),
        ("submit", ()),
        ("cleanup", ()),
        ("doctor", ()),
        ("land", ()),
        ("close", ()),
        ("restart", ("@",)),
        ("unlink", ("@",)),
    ],
)
def test_commands_do_not_crash_in_empty_repo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    args: tuple[str, ...],
) -> None:
    repo = _init_empty_repo(tmp_path)
    config_path = _write_config(tmp_path)

    exit_code = run_main(repo, config_path, command, *args)
    captured = capsys.readouterr()

    assert exit_code in (0, 1)
    _assert_no_traceback(captured)


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("status", ()),
        ("submit", ()),
        ("cleanup", ("--rebase",)),
        ("unlink", ("@-",)),
    ],
)
def test_stack_commands_fail_closed_for_disconnected_roots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    args: tuple[str, ...],
) -> None:
    repo = _init_disconnected_root_repo(tmp_path)
    _add_github_like_remote(repo)
    config_path = _write_config(tmp_path)

    exit_code = run_main(repo, config_path, command, *args)
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "root commit" in combined
    assert "trunk()" in combined
    _assert_no_traceback(captured)


@pytest.mark.parametrize("command", ["status", "submit"])
def test_commands_report_non_github_remote_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "feature 1", "feature-1.txt")
    run_command(
        ["jj", "git", "remote", "add", "origin", "ssh://example.test/not-github.git"],
        repo,
    )
    config_path = _write_config(tmp_path)

    exit_code = run_main(repo, config_path, command)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert_output_contains(captured.out + captured.err, "Use a GitHub remote URL.")
    _assert_no_traceback(captured)


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("status", ()),
        ("submit", ()),
        ("cleanup", ("--rebase",)),
        ("unlink", ("@-",)),
    ],
)
def test_stack_commands_reject_merge_commits_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    args: tuple[str, ...],
) -> None:
    repo = _init_merge_commit_repo(tmp_path)
    _add_github_like_remote(repo)
    config_path = _write_config(tmp_path)

    exit_code = run_main(repo, config_path, command, *args)
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "merge commits are not supported" in combined
    _assert_no_traceback(captured)


def _init_empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    run_command(["jj", "config", "set", "--repo", "user.name", "Test User"], repo)
    run_command(["jj", "config", "set", "--repo", "user.email", "test@example.com"], repo)
    return repo


def _init_disconnected_root_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path, configure_trunk=False)
    run_command(["jj", "bookmark", "create", "main", "-r", "@-"], repo)
    run_command(["jj", "new", "root()"], repo)
    run_command(["jj", "bookmark", "create", "trunk-alias", "-r", "@"], repo)
    run_command(
        ["jj", "config", "set", "--repo", 'revset-aliases."trunk()"', "trunk-alias"],
        repo,
    )
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature on main", "feature.txt")
    return repo


def _init_merge_commit_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path)
    base_commit = _parent_commit_id(repo)

    commit_file(repo, "left", "left.txt")
    left_commit = _parent_commit_id(repo)

    run_command(["jj", "new", base_commit], repo)
    commit_file(repo, "right", "right.txt")
    right_commit = _parent_commit_id(repo)

    run_command(["jj", "new", left_commit, right_commit], repo)
    commit_file(repo, "merge", "merge.txt")
    return repo


def _parent_commit_id(repo: Path) -> str:
    completed = run_command(
        ["jj", "log", "--no-graph", "-r", "@-", "-T", "commit_id"],
        repo,
    )
    return completed.stdout.strip()


def _add_github_like_remote(repo: Path) -> None:
    run_command(
        [
            "jj",
            "git",
            "remote",
            "add",
            "origin",
            "https://github.test/octo-org/stacked-review.git",
        ],
        repo,
    )


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "jj-review-config.toml"
    config_path.write_text("[jj-review]\n", encoding="utf-8")
    return config_path


def _assert_no_traceback(captured) -> None:
    combined = captured.out + captured.err
    assert "Traceback" not in combined
    assert "AssertionError" not in combined
