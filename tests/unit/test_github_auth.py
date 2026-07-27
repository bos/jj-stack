from __future__ import annotations

from types import SimpleNamespace

import pytest

import jj_stack.github.auth as github_auth_module
from jj_stack.github.auth import github_token, github_token_from_env


def test_github_token_from_env_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("GH_TOKEN", "gh-token")

    assert github_token_from_env() == "github-token"

    monkeypatch.delenv("GITHUB_TOKEN")

    assert github_token_from_env() == "gh-token"


def test_github_token_falls_back_to_default_gh_cli_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls: list[list[str]] = []

    def fake_run(command, *, capture_output, check, text):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="gh-token\n")

    monkeypatch.setattr(github_auth_module.subprocess, "run", fake_run)

    assert github_token() == "gh-token"
    assert calls == [["gh", "auth", "token"]]
