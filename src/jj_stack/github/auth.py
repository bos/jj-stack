"""GitHub authentication token discovery helpers."""

from __future__ import annotations

import os
import subprocess
from urllib.parse import urlparse


def github_token_from_env() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def github_token_for_base_url(base_url: str) -> str | None:
    if token := github_token_from_env():
        return token
    if hostname := _github_hostname_from_api_base_url(base_url):
        return _github_token_from_gh_cli(hostname)
    return None


def _github_hostname_from_api_base_url(base_url: str) -> str | None:
    hostname = urlparse(base_url).hostname
    if hostname is None:
        return None
    if hostname == "api.github.com":
        return "github.com"
    if hostname.startswith("api."):
        return hostname[4:]
    return hostname


def _github_token_from_gh_cli(hostname: str) -> str | None:
    try:
        completed = subprocess.run(
            ["gh", "auth", "token", "--hostname", hostname],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    if not token:
        return None
    return token
