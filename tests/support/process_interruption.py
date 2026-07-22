"""Run one land or recovery command in a disposable process.

The parent integration test owns the repository and a pickled fake-GitHub service. A faulting
child persists the external service immediately after the accepted effect, then calls
``os._exit``. That bypasses Python cleanup and leaves the next child to recover only from the
repository, remote, GitHub, and sparse tracking facts that survived process death.
"""

from __future__ import annotations

import importlib
import os
import pickle
import sys
from pathlib import Path
from typing import Never

import httpxyz

from jj_stack.cli import main
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore
from tests.support.fake_github import FakeGithubRepository, FakeGithubState, create_app

FAULT_EXIT = 86
_COMMAND_MODULES = (
    "jj_stack.commands.land.command",
    "jj_stack.commands.submit.command",
    "jj_stack.commands.sync",
    "jj_stack.review.status",
)


def _persist(fake_repo: FakeGithubRepository, path: Path) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(pickle.dumps(fake_repo))
    temporary.replace(path)


def _die(fake_repo: FakeGithubRepository, path: Path) -> Never:
    _persist(fake_repo, path)
    os._exit(FAULT_EXIT)


def _wire_fake_github(fake_repo: FakeGithubRepository) -> None:
    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, repository: GithubRepoAddress) -> GithubClient:
        return GithubClient(
            httpxyz.AsyncClient(
                base_url="https://api.github.test",
                transport=httpxyz.ASGITransport(app=app),
            ),
            repository=repository,
        )

    def parse_github_repo(*_args, **_kwargs) -> GithubRepoAddress:
        return GithubRepoAddress(
            host="github.test",
            owner=fake_repo.owner,
            repo=fake_repo.name,
        )

    resolution = importlib.import_module("jj_stack.github.resolution")
    resolution.__dict__["parse_github_repo"] = parse_github_repo
    for module_name in _COMMAND_MODULES:
        module = importlib.import_module(module_name)
        if hasattr(module, "build_github_client"):
            module.__dict__["build_github_client"] = build_github_client
        if hasattr(module, "parse_github_repo"):
            module.__dict__["parse_github_repo"] = parse_github_repo
        if hasattr(module, "require_github_repo"):
            module.__dict__["require_github_repo"] = parse_github_repo


def _install_fault(fault: str, fake_repo: FakeGithubRepository, state_path: Path) -> None:
    if fault == "trunk_push":
        original_push = JjClient.push_bookmark_with_lease

        def push_then_die(self, **kwargs) -> None:
            original_push(self, **kwargs)
            if kwargs["bookmark"] == "main":
                _die(fake_repo, state_path)

        JjClient.push_bookmark_with_lease = push_then_die
        return
    if fault == "accepted_merge":
        original_merge = GithubClient.merge_pull_request

        async def merge_then_die(self, **kwargs) -> None:
            await original_merge(self, **kwargs)
            _die(fake_repo, state_path)

        GithubClient.merge_pull_request = merge_then_die
        return
    if fault == "retirement_save":

        def die_before_retirement(self, *args, **kwargs):
            _die(fake_repo, state_path)

        ReviewStateStore.retire_review = die_before_retirement
        return
    raise AssertionError(f"unknown process fault: {fault}")


def _command(fault: str, mode: str) -> tuple[str, ...]:
    if mode == "fault":
        return ("land", "--via", "merge") if fault == "accepted_merge" else ("land",)
    if mode == "recover":
        return ("sync",) if fault == "accepted_merge" else ("sync", "--all")
    raise AssertionError(f"unknown process mode: {mode}")


def run(argv: list[str]) -> int:
    fault, mode, repo_arg, config_arg, service_arg, state_home_arg = argv
    repo = Path(repo_arg)
    config_path = Path(config_arg)
    service_path = Path(service_arg)
    os.environ["XDG_STATE_HOME"] = state_home_arg
    fake_repo = pickle.loads(service_path.read_bytes())
    _wire_fake_github(fake_repo)
    if mode == "fault":
        _install_fault(fault, fake_repo, service_path)
    command = _command(fault, mode)
    exit_code = main(
        [
            "--config-file",
            str(config_path),
            "--repository",
            str(repo),
            *command,
        ]
    )
    _persist(fake_repo, service_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
