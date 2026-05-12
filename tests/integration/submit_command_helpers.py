from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import httpxyz

from jj_review.cli import main
from jj_review.github.client import GithubClient
from jj_review.github.resolution import ParsedGithubRepo

from ..support.fake_github import FakeGithubRepository
from ..support.integration_helpers import (
    configure_fake_github_environment,
    run_command,
    write_fake_github_config,
)


def configure_submit_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
    *,
    extra_config_lines: list[str] | None = None,
) -> Path:
    return configure_fake_github_environment(
        command_modules=(
            "jj_review.commands.submit.command",
            "jj_review.commands.relink",
            "jj_review.commands.close",
            "jj_review.commands.close_orphan",
            "jj_review.commands.cleanup.command",
            "jj_review.commands.land.command",
            "jj_review.commands.list_",
            "jj_review.review.status",
        ),
        fake_repo=fake_repo,
        extra_config_lines=extra_config_lines,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


def approve_pull_requests(fake_repo: FakeGithubRepository, *pull_numbers: int) -> None:
    for pull_number in pull_numbers:
        fake_repo.create_pull_request_review(
            pull_number=pull_number,
            reviewer_login=f"reviewer-{pull_number}",
            state="APPROVED",
        )


def issue_comments(fake_repo: FakeGithubRepository, issue_number: int):
    return fake_repo.issue_comments.get(issue_number, [])


def read_remote_ref(remote: Path, bookmark: str) -> str:
    completed = run_command(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{bookmark}"],
        remote.parent,
    )
    return completed.stdout.strip()


def remote_refs(remote: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        check=False,
        cwd=remote.parent,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(
            "['git', '--git-dir', "
            f"{str(remote)!r}, 'show-ref', '--heads'] failed:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    refs: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        commit_id, ref_name = line.split(" ", maxsplit=1)
        refs[ref_name] = commit_id
    return refs


def run_main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    argv = ["--config-file", str(config_path), "--repository", str(repo), command]
    argv.extend(command_args)
    return main(argv)


def patch_github_client_builders(
    monkeypatch,
    *,
    app,
    fake_repo: FakeGithubRepository,
    modules: tuple[str, ...],
    client_type: type[GithubClient] = GithubClient,
    concurrency_limits: dict[str, int] | None = None,
) -> None:
    def build_github_client(*, base_url: str) -> GithubClient:
        return client_type(base_url=base_url, transport=httpxyz.ASGITransport(app=app))

    def parse_github_repo(*_args, **_kwargs) -> ParsedGithubRepo:
        return ParsedGithubRepo(host="github.test", owner=fake_repo.owner, repo=fake_repo.name)

    resolution_module = importlib.import_module("jj_review.github.resolution")
    monkeypatch.setattr(resolution_module, "parse_github_repo", parse_github_repo)
    for module in modules:
        module_object = importlib.import_module(module)
        monkeypatch.setattr(
            module_object,
            "build_github_client",
            build_github_client,
            raising=False,
        )
        monkeypatch.setattr(module_object, "parse_github_repo", parse_github_repo, raising=False)
        monkeypatch.setattr(
            module_object, "require_github_repo", parse_github_repo, raising=False
        )
    if concurrency_limits is None:
        return
    for module, limit in concurrency_limits.items():
        monkeypatch.setattr(f"{module}._GITHUB_INSPECTION_CONCURRENCY", limit)


def write_config(
    tmp_path: Path, fake_repo: FakeGithubRepository, *, extra_lines: list[str] | None = None
) -> Path:
    return write_fake_github_config(tmp_path, fake_repo, extra_lines=extra_lines)
