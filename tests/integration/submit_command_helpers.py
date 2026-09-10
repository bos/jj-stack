from __future__ import annotations

from pathlib import Path

from jj_stack.cli import main

from ..support.fake_github import FakeGithubRepo
from ..support.integration_helpers import (
    configure_fake_github_environment,
    run_command,
)


def configure_submit_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepo,
    *,
    extra_config_lines: list[str] | None = None,
) -> Path:
    return configure_fake_github_environment(
        fake_repo=fake_repo,
        extra_config_lines=extra_config_lines,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


def issue_comments(fake_repo: FakeGithubRepo, issue_number: int):
    return fake_repo.issue_comments.get(issue_number, [])


def read_remote_ref(remote: Path, bookmark: str) -> str:
    completed = run_command(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{bookmark}"],
        remote.parent,
    )
    return completed.stdout.strip()


def run_main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    argv = ["--config-file", str(config_path), "--repository", str(repo), command]
    argv.extend(command_args)
    return main(argv)
