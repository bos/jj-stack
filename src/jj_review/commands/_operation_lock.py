"""Command helpers for repo-scoped mutation locking."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from jj_review.bootstrap import CommandContext
from jj_review.state.operation_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    OperationLock,
    acquire_operation_lock,
)


@contextmanager
def mutating_command_lock(
    *,
    command: str,
    context: CommandContext,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[OperationLock]:
    """Hold the repo operation lock for one mutating command."""

    with acquire_operation_lock(
        context.state_store.require_writable(),
        command=command,
        timeout=timeout,
    ) as lock:
        yield lock
