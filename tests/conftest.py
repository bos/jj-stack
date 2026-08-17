from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

import jj_stack.console as console
from jj_stack.review_namespace import install_review_namespace

pytest_plugins = ["tests.support.pytest_concurrency"]


@pytest.fixture(autouse=True)
def _install_default_review_namespace() -> None:
    """Reset the process-wide review policy before each test."""

    install_review_namespace("jj-stack")


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
