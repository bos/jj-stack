from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests import run_live_github as live_runner


def test_cleanup_refuses_a_repository_without_the_live_run_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = "20260821-120000-deadbeef"
    repo = live_runner.DisposableRepo(
        owner="octocat",
        name=f"{live_runner.DISPOSABLE_PREFIX}{marker}",
        marker=marker,
    )
    commands: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        commands.append(tuple(command))
        metadata = json.dumps(
            {
                "full_name": repo.full_name,
                "private": True,
                "description": "somebody changed the run marker",
            }
        )
        return subprocess.CompletedProcess(command, 0, metadata, "")

    monkeypatch.setattr(live_runner.subprocess, "run", run)
    suite = live_runner.LiveGithubSuite(
        deadline=0,
        env={},
        repo=repo,
        root=tmp_path,
    )

    with pytest.raises(live_runner.LiveTestError, match="refusing to delete"):
        suite._delete_remote()
    assert all(command[:3] != ("gh", "repo", "delete") for command in commands)
