"""GitHub authentication token discovery helpers."""

from __future__ import annotations

import os
import subprocess


def github_token_from_env() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def github_token() -> str | None:
    if token := github_token_from_env():
        return token
    return _github_token_from_gh_cli()


def _github_token_from_gh_cli() -> str | None:
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
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
