from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

import jj_stack.console as console
from jj_stack.pr_branch_namespace import install_pr_branch_namespace
from tests.support import integration_helpers

pytest_plugins = ["tests.support.pytest_concurrency"]


@pytest.fixture(autouse=True)
def _install_default_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a fixed terminal environment it can override explicitly."""

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "25")
    for name in ("COLORTERM", "FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _install_default_pr_branch_namespace() -> None:
    """Reset the process-wide PR branch policy before each test."""

    install_pr_branch_namespace("jj-stack")


@pytest.fixture(autouse=True)
def _no_console_writes_while_spinner_active(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fail any test that writes ordinary console output before its spinner stops."""

    active_spinners = 0
    original_spinner = console.spinner

    @contextmanager
    def checked_spinner(*, description: str, report_changes: bool = False):
        nonlocal active_spinners
        with original_spinner(description=description, report_changes=report_changes) as handle:
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


@pytest.fixture(autouse=True, scope="session")
def _share_repo_templates_across_workers(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Let all xdist workers reuse one set of cached repo templates.

    Each worker's base temp lives under the session temp root (`popen-gwN`
    subdirectories), so the parent directory is shared across workers and
    outlives no test. Without this, every worker rebuilds every template.
    """

    base = tmp_path_factory.getbasetemp()
    root = base.parent if base.name.startswith("popen-") else base
    integration_helpers.set_shared_template_root(root / "jj-stack-templates")
