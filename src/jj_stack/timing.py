"""Per-call timing behind `--time-output`.

Every jj, git, and gh subprocess, every GitHub request, and every spinner phase runs inside
`timed`, which logs its duration at debug level and adds it to a per-kind total that `summary`
reports when the command finishes. Concurrent GitHub requests overlap, so their total can exceed
the wall time. The jj probes that run before logging is configured are totalled but not logged.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter

import jj_stack

logger = logging.getLogger(__name__)

_LABEL_LIMIT = 120


@dataclass(slots=True)
class _Total:
    count: int = 0
    seconds: float = 0.0


_lock = threading.Lock()
_totals: dict[str, _Total] = {}


@contextmanager
def timed(kind: str, label: str) -> Iterator[None]:
    """Time one external call, logging it and adding it to the total for `kind`."""

    start = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter() - start
        with _lock:
            total = _totals.setdefault(kind, _Total())
            total.count += 1
            total.seconds += elapsed
        logger.debug("%s %.3fs %s", kind, elapsed, " ".join(label.split())[:_LABEL_LIMIT])


def summary(*, imports_seconds: float) -> str:
    """Report wall time, import time, and the per-kind call totals."""

    parts = [
        f"wall {perf_counter() - jj_stack.PROCESS_START:.2f}s",
        f"imports {imports_seconds:.2f}s",
    ]
    with _lock:
        parts.extend(
            f"{kind} {total.count}\u00d7 {total.seconds:.2f}s"
            for kind, total in sorted(_totals.items())
        )
    return "; ".join(parts)
