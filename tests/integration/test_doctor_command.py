from __future__ import annotations

from pathlib import Path

import jj_stack.commands.doctor as doctor_mod
from jj_stack.github.client import GithubClient
from jj_stack.jj.client import JjClient
from jj_stack.pr_branch_namespace import current_pr_branch_namespace

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    expose_pr_branch_namespace,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    patch_github_client_builders,
    run_command,
    selected_stack,
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
    """Set up a fake GitHub environment for doctor integration tests."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    # Provide a fake token so the auth check passes without a real gh CLI or env var.
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests")

    patch_github_client_builders(
        monkeypatch,
        app=create_app(FakeGithubState.single_repo(fake_repo)),
        fake_repo=fake_repo,
        client_type=client_type,
    )

    return write_fake_github_config(tmp_path)


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


def test_doctor_warns_about_a_local_pr_bookmark_it_does_not_forget(
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
    assert f"visible bookmarks remain: {branch}" in output


def test_doctor_fix_forgets_fetched_pr_bookmarks_and_leftovers(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)
    change = selected_stack(repo).head
    branch = fake_repo.prs[1].head_ref
    expose_pr_branch_namespace(repo)
    run_command(["jj", "git", "fetch", "--remote", "origin"], repo)
    run_command(["jj", "bookmark", "create", "jj-stack-tmp/checkout", "-r", "@-"], repo)

    assert run_main(repo, config_path, "doctor") == 0
    output = " ".join(capsys.readouterr().out.split())
    assert f"{branch} came from a fetch" in output
    assert "jj-stack doctor --fix" in output

    assert run_main(repo, config_path, "doctor", "--fix") == 0
    output = " ".join(capsys.readouterr().out.split())
    client = JjClient(repo)

    assert f"forgot {branch}" in output
    assert client.visible_pr_bookmark_targets() == {}
    assert client.pr_branch_temp_artifacts().bookmark_targets == ()
    run_command(["jj", "describe", "-r", change.change_id, "-m", "editable again"], repo)


def test_doctor_distinguishes_duplicate_fetch_exclusions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)
    client = JjClient(repo)
    client.ensure_pr_branch_fetch_isolation(remote="origin")
    run_command(
        [
            "git",
            "config",
            "--add",
            "remote.origin.fetch",
            current_pr_branch_namespace().fetch_refspec,
        ],
        repo,
    )

    exit_code = run_main(repo, config_path, "doctor")
    output = " ".join(capsys.readouterr().out.split())

    assert exit_code == 0
    glob = current_pr_branch_namespace().branch_glob
    assert f"fetch rule that skips {glob} branches is duplicated" in output
    assert "keep one with jj-stack doctor --fix" in output
    assert "missing" not in output

    assert run_main(repo, config_path, "doctor", "--fix") == 0
    refspecs = run_command(
        ["git", "config", "--get-all", "remote.origin.fetch"], repo
    ).stdout.splitlines()
    assert refspecs.count(current_pr_branch_namespace().fetch_refspec) == 1
    assert "+refs/heads/*:refs/remotes/origin/*" in refspecs


def test_doctor_fix_applies_the_pr_branch_fetch_exclusion(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    assert run_main(repo, config_path, "doctor") == 0
    output = " ".join(capsys.readouterr().out.split())
    glob = current_pr_branch_namespace().branch_glob
    assert f"jj git fetch does not skip {glob} branches" in output
    assert "multiple" not in output
    assert "jj-stack doctor --fix" in output
    assert "without --dry-run" not in output

    assert run_main(repo, config_path, "doctor", "--fix") == 0
    fixed_output = " ".join(capsys.readouterr().out.split())
    assert "fixed" in fixed_output

    # The repair sticks, so a plain run now passes and offers no advice.
    assert run_main(repo, config_path, "doctor") == 0
    rerun_output = " ".join(capsys.readouterr().out.split())
    assert "jj-stack doctor --fix" not in rerun_output
    assert "stacked pull requests available" in rerun_output


def test_doctor_fails_without_push_access_to_the_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A clone of a repo the token cannot push to, such as an unforked upstream, cannot
    receive PR branches, and GitHub cannot stack PRs whose branches live in a fork."""
    repo, fake_repo = init_fake_github_repo(tmp_path)
    fake_repo.push_permission = False
    config_path = _configure_doctor_environment(monkeypatch, tmp_path, fake_repo)

    exit_code = run_main(repo, config_path, "doctor")
    output = " ".join(capsys.readouterr().out.split())

    assert exit_code == 1
    assert f"no push access to {fake_repo.full_name}" in output
    assert "GitHub stacks cannot span forks" in output
    # The remaining GitHub checks still run, so one report shows the whole picture.
    assert "stacked pull requests available" in output


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
