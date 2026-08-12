"""Check whether jj-stack has stored tracking in this local repository.

The command is a silent predicate for scripts and coding-agent instructions. It exits 0 when a
valid jj-stack tracking file exists and 1 when none exists. It does not inspect the working copy,
read GitHub, or create tracking. A repository or tracking error is reported on stderr and exits
11, so callers can distinguish an error from a clean negative result.
"""

from __future__ import annotations

from pathlib import Path

from jj_stack.bootstrap import resolve_repo_root
from jj_stack.errors import CliError, ProbeError
from jj_stack.state.store import ReviewStateStore

HELP = "Check whether jj-stack is in use in this local repository"


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
