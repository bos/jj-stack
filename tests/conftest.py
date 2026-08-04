from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

import jj_stack.console as console
import jj_stack.review.branches as review_branches
from jj_stack.config import DEFAULT_BRANCH_PREFIX

pytest_plugins = ["tests.support.pytest_concurrency"]


@pytest.fixture(autouse=True)
def _review_namespace(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test the namespace a real invocation has, and keep it out of the next test.

    `bootstrap` installs it process-wide without restoring, so a test that runs the real CLI would
    otherwise leave its prefix set for every later test sharing the xdist worker, making the tests
    that depend on an installed namespace vary with the random test order.
    """

    monkeypatch.setattr(review_branches, "_prefix", DEFAULT_BRANCH_PREFIX)
    yield


@pytest.fixture(autouse=True)
def _no_console_writes_while_spinner_active(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fail any test that writes ordinary console output before its spinner stops."""

    active_spinners = 0
    original_spinner = console.spinner

    @contextmanager
    def checked_spinner(*, description: str):
        nonlocal active_spinners
        with original_spinner(description=description) as handle:
            active_spinners += 1
            try:
                yield handle
            finally:
                active_spinners -= 1

    def checked_write(name: str, write: Callable):
        def guard(*args, **kwargs):
            assert not active_spinners, f"console.{name} wrote while a spinner was active"
            return write(*args, **kwargs)

        return guard

    monkeypatch.setattr(console, "spinner", checked_spinner)
    for name in ("error", "note", "output", "stderr_output", "warning"):
        monkeypatch.setattr(console, name, checked_write(name, getattr(console, name)))
    yield
