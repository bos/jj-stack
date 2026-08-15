"""Check whether this repository contains valid `jj-stack` tracking data.

Intended for scripts and automation, the command prints nothing and exits 0 when `jj-stack` is in
use, 1 when it is not, and 11 for repository or tracking errors. It does not inspect the working
copy, read GitHub, or create tracking.
"""

from __future__ import annotations

from pathlib import Path

from jj_stack.bootstrap import resolve_repo_root
from jj_stack.errors import CliError, ProbeError
from jj_stack.state.store import ReviewStateStore

HELP = "Check whether this repository uses `jj-stack`"


def in_use(*, repository: Path | None) -> int:
    """CLI entrypoint for `in-use`."""

    start = Path.cwd() if repository is None else repository
    try:
        if repository is not None and not repository.exists():
            raise CliError(f"Repository path does not exist: {repository}")
        if repository is not None and not repository.is_dir():
            raise CliError(f"Repository path is not a directory: {repository}")
        repo_root = resolve_repo_root(start)
        return 0 if ReviewStateStore.for_repo(repo_root).is_in_use() else 1
    except CliError as error:
        raise ProbeError(error.message, hint=error.hint) from error
