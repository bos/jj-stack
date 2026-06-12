from __future__ import annotations

from types import SimpleNamespace

import pytest

import jj_stack.github.auth as github_auth_module
from jj_stack.github.auth import github_token_for_base_url, github_token_from_env


def test_github_token_from_env_prefers_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("GH_TOKEN", "gh-token")

    assert github_token_from_env() == "github-token"


def test_github_token_from_env_falls_back_to_gh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gh-token")

    assert github_token_from_env() == "gh-token"


def test_github_token_for_base_url_falls_back_to_gh_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls: list[list[str]] = []

    def fake_run(command, *, capture_output, check, text):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="gh-token\n")

    monkeypatch.setattr(github_auth_module.subprocess, "run", fake_run)

    assert github_token_for_base_url("https://api.github.com") == "gh-token"
    assert calls == [["gh", "auth", "token", "--hostname", "github.com"]]
