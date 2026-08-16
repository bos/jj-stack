from __future__ import annotations

from pathlib import Path

import httpxyz

import jj_stack.commands.doctor as doctor_mod
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjClient

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    TEST_REVIEW_NAMESPACE,
    init_fake_github_repo,
    run_command,
    write_fake_github_config,
)
from .submit_command_helpers import run_main


def _configure_doctor_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo,
    *,
    client_type: type[GithubClient] = GithubClient,
) -> Path:
    """Set up a fake GitHub environment for doctor integration tests.

    Patches build_github_client and parse_github_repo in the doctor module so that
    connectivity checks go to the fake GitHub server instead of the real API.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    # Provide a fake token so the auth check passes without a real gh CLI or env var.
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests")

    app = create_app(FakeGithubState.single_repository(fake_repo))

    def build_github_client(*, repository: GithubRepoAddress) -> GithubClient:
        return client_type(
            httpxyz.AsyncClient(
                base_url="https://api.github.test",
                transport=httpxyz.ASGITransport(app=app),
            ),
            repository=repository,
        )

    monkeypatch.setattr(doctor_mod, "build_github_client", build_github_client)
    monkeypatch.setattr(
        doctor_mod,
        "parse_github_repo",
        lambda remote: GithubRepoAddress(owner=fake_repo.owner, repo=fake_repo.name),
    )

    return write_fake_github_config(tmp_path, fake_repo)


def test_doctor_exits_zero_for_healthy_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)
    JjClient(repo).ensure_review_fetch_isolation(
        namespace=TEST_REVIEW_NAMESPACE,
        remote="origin",
    )

    exit_code = run_main(repo, config_path, "doctor")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "GitHub auth" in captured.out
    assert "Stacks API available" in captured.out
    assert "checkout/sync leftovers" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_doctor_reports_when_github_stacks_are_unavailable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)

    class StacksUnavailableClient(GithubClient):
        async def list_stacks(self):
            raise doctor_mod.GithubClientError("Not Found", status_code=404)

    config_path = _configure_doctor_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        client_type=StacksUnavailableClient,
    )

    assert run_main(repo, config_path, "doctor") == 1
    output = capsys.readouterr().out
    assert "GitHub stacked pull requests are unavailable" in output
    assert "https://gh.io/stacksbeta" in output
    assert output.count("https://gh.io/stacksbeta") == 1


def test_doctor_warns_about_an_imported_review_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)
    branch = "jj-stack/feature-abcdefgh"
    run_command(["jj", "bookmark", "create", branch, "-r", "@"], repo)

    exit_code = run_main(repo, config_path, "doctor")
    output = " ".join(capsys.readouterr().out.split())

    assert exit_code == 0
    assert f"Visible bookmarks remain: {branch}" in output


def test_doctor_reports_runnable_missing_fetch_isolation_recovery(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "doctor")
    output = " ".join(capsys.readouterr().out.split())

    assert exit_code == 0
    assert f"missing {TEST_REVIEW_NAMESPACE.fetch_refspec} exclusion" in output
    assert "multiple" not in output
    assert "jj-stack doctor --fix" in output
    assert "without --dry-run" not in output


def test_doctor_distinguishes_duplicate_fetch_exclusions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)
    client = JjClient(repo)
    client.ensure_review_fetch_isolation(namespace=TEST_REVIEW_NAMESPACE, remote="origin")
    run_command(
        ["git", "config", "--add", "remote.origin.fetch", TEST_REVIEW_NAMESPACE.fetch_refspec],
        repo,
    )

    exit_code = run_main(repo, config_path, "doctor")
    output = " ".join(capsys.readouterr().out.split())

    assert exit_code == 0
    assert f"multiple {TEST_REVIEW_NAMESPACE.fetch_refspec} exclusions" in output
    assert "keep one with jj-stack doctor --fix" in output
    assert "missing" not in output


def test_doctor_fix_applies_the_review_fetch_exclusion(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    assert run_main(repo, config_path, "doctor") == 0
    capsys.readouterr()

    assert run_main(repo, config_path, "doctor", "--fix") == 0
    fixed_output = " ".join(capsys.readouterr().out.split())
    assert "fixed" in fixed_output

    # The repair sticks, so a plain run now passes and offers no advice.
    assert run_main(repo, config_path, "doctor") == 0
    rerun_output = " ".join(capsys.readouterr().out.split())
    assert "jj-stack doctor --fix" not in rerun_output


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
    monkeypatch.setattr(doctor_mod, "github_token", lambda: None)

    exit_code = run_main(repo, config_path, "doctor")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub auth" in captured.out
    # Connectivity and trunk branch should appear as skipped
    assert "connectivity" in captured.out
    assert "trunk branch" in captured.out
    assert "prior check failed" in captured.out
