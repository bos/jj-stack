from __future__ import annotations

from pathlib import Path

import httpxyz

import jj_review.commands.doctor as doctor_mod
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.resolution import ParsedGithubRepo

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    init_fake_github_repo,
    write_fake_github_config,
)
from .submit_command_helpers import run_main


def _configure_doctor_environment(monkeypatch, tmp_path: Path, fake_repo) -> Path:
    """Set up a fake GitHub environment for doctor integration tests.

    Patches build_github_client and parse_github_repo in the doctor module so that
    connectivity checks go to the fake GitHub server instead of the real API.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    # Provide a fake token so the auth check passes without a real gh CLI or env var.
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests")

    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, base_url: str, token: str | None = None) -> GithubClient:
        return GithubClient(base_url=base_url, transport=httpxyz.ASGITransport(app=app))

    monkeypatch.setattr(doctor_mod, "build_github_client", build_github_client)
    monkeypatch.setattr(
        doctor_mod,
        "parse_github_repo",
        lambda remote: ParsedGithubRepo(
            host="github.test", owner=fake_repo.owner, repo=fake_repo.name
        ),
    )

    return write_fake_github_config(tmp_path, fake_repo)


def test_doctor_exits_zero_for_healthy_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "doctor")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "GitHub auth" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_doctor_shows_skipped_checks_when_remote_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path, with_remote=False)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "doctor")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no Git remotes" in captured.out
    # Dependent checks should appear as skipped, not absent
    assert "GitHub remote" in captured.out
    assert "GitHub auth" in captured.out
    assert "connectivity" in captured.out
    assert "trunk branch" in captured.out
    assert "prior check failed" in captured.out


def test_doctor_fails_when_github_token_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    # Remove the token that _configure_doctor_environment sets.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(doctor_mod, "github_token_for_base_url", lambda base_url: None)

    exit_code = run_main(repo, config_path, "doctor")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub auth" in captured.out
    # Connectivity and trunk branch should appear as skipped
    assert "connectivity" in captured.out
    assert "trunk branch" in captured.out
    assert "prior check failed" in captured.out


def test_doctor_reports_repo_access_failure_without_network_hint(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    class FailingGithubClient(GithubClient):
        async def get_repository(self, owner: str, repo: str):
            raise GithubClientError(
                'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
                status_code=404,
            )

    monkeypatch.setattr(
        doctor_mod,
        "build_github_client",
        lambda *, base_url, token=None: FailingGithubClient(base_url=base_url),
    )

    exit_code = run_main(repo, config_path, "doctor")
    captured = capsys.readouterr()
    normalized_out = " ".join(captured.out.split())

    assert exit_code == 1
    assert "connectivity" in normalized_out
    assert "repo not found or inaccessible" in normalized_out
    assert "check network connectivity" not in normalized_out
    assert "documentation_url" not in normalized_out
