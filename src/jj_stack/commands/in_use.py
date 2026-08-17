"""Check whether this repo contains valid `jj-stack` tracking data.

Intended for scripts and automation, the command prints nothing and exits 0 when `jj-stack` is in
use, 1 when it is not, and 11 for repo or tracking errors. It does not inspect the working
copy, read GitHub, or create tracking.
"""

from __future__ import annotations

from pathlib import Path

from jj_stack.bootstrap import resolve_repo_root
from jj_stack.errors import CliError, ProbeError
from jj_stack.state.store import TrackingStore

HELP = "Check whether this repo uses `jj-stack`"


def in_use(*, repo: Path | None) -> int:
    """CLI entrypoint for `in-use`."""

    start = Path.cwd() if repo is None else repo
    try:
        if repo is not None and not repo.exists():
            raise CliError(f"Repo path does not exist: {repo}")
        if repo is not None and not repo.is_dir():
            raise CliError(f"Repo path is not a directory: {repo}")
        repo_root = resolve_repo_root(start)
        return 0 if TrackingStore.for_repo(repo_root).is_in_use() else 1
    except CliError as error:
        raise ProbeError(error.message, hint=error.hint) from error
