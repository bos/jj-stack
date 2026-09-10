"""Runtime bootstrap helpers for CLI commands."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import jj_stack
import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.config import AppConfig, load_config
from jj_stack.errors import CliError
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient
from jj_stack.jj.settings import read_jj_settings
from jj_stack.pr_branch_namespace import install_pr_branch_namespace
from jj_stack.state.store import TrackingStore

_MINIMUM_JJ_VERSION = (0, 45, 1)
_jj_version_verified = False

time_output_active: bool = False


class _ElapsedFormatter(logging.Formatter):
    """Prepend the `--time-output` prefix when it's active."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if not time_output_active:
            return base
        elapsed = time.perf_counter() - jj_stack.PROCESS_START
        return console.style_time_prefix(f"[{elapsed:0.6f}] ") + base


@dataclass(slots=True, frozen=True)
class CommandContext:
    """Typed runtime state shared by command handlers."""

    config: AppConfig
    jj_client: JjClient
    repo_root: Path
    state_store: TrackingStore


def bootstrap_context(
    *,
    repo: Path | None,
    cli_args: JjCliArgs,
    debug: bool,
) -> CommandContext:
    """Resolve the repo, read jj's config once, and initialize the console and logging."""

    repo = repo.resolve() if repo is not None else None
    validate_repo_path(repo)
    check_jj_version()
    repo_root = resolve_repo_root(repo or Path.cwd())
    settings = read_jj_settings(cwd=repo_root, cli_args=cli_args)
    console.adopt_jj_config(color=settings.string("ui", "color"), colors=settings.table("colors"))
    jj_client = JjClient(repo_root, cli_args=cli_args, settings=settings)
    config = load_config(settings=settings)
    install_pr_branch_namespace(config.branch_prefix)
    jj_client.enable_initial_working_copy_snapshot()
    configure_logging(debug=debug, configured_level=config.logging.level)
    return CommandContext(
        config=config,
        jj_client=jj_client,
        repo_root=repo_root,
        state_store=TrackingStore.for_repo(repo_root),
    )


def configure_logging(*, debug: bool, configured_level: str) -> None:
    """Apply process-wide logging defaults for the current command."""

    root_level = logging.getLevelNamesMapping()[configured_level]
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
        level=root_level,
    )
    formatter = _ElapsedFormatter("%(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    app_level = logging.DEBUG if debug else root_level
    logging.getLogger("jj_stack").setLevel(app_level)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def resolve_repo_root(start_dir: Path) -> Path:
    """Resolve the jj workspace root by walking up from `start_dir`.

    Mirrors what `jj root` does internally (searches for the nearest ancestor
    containing a `.jj` directory) without forking a subprocess. Couples to
    jj's on-disk layout: every workspace root is assumed to hold `.jj` as a
    directory, as jj does today. If jj ever grows a `.jj`-as-file pointer
    (analogous to git's submodule/worktree `.git` files), this needs to
    learn about that form.
    """

    try:
        resolved = start_dir.resolve(strict=False)
    except OSError as error:
        raise CliError(f"Could not resolve path {start_dir}: {error}") from error

    for candidate in (resolved, *resolved.parents):
        if (candidate / ".jj").is_dir():
            return candidate
    raise CliError(f"Not inside a jj workspace (from {start_dir}).")


def check_jj_version() -> None:
    """Verify that the installed `jj` meets the minimum required version.

    Raises `CliError` if `jj` is absent, if its version string cannot be parsed,
    or if the installed version is older than the minimum. A successful check
    holds for the lifetime of the process and is not repeated.
    """

    global _jj_version_verified
    if _jj_version_verified:
        return
    try:
        completed = subprocess.run(
            ["jj", "--version"],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as error:
        raise CliError(t"{ui.cmd('jj')} is not installed or is not on PATH.") from error

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CliError(t"{ui.cmd('jj --version')} failed: {message}")

    minimum_version = ".".join(str(part) for part in _MINIMUM_JJ_VERSION)
    version = _parse_jj_version(completed.stdout.strip())
    if version is None:
        raise CliError(
            t"Could not parse {ui.cmd('jj --version')} output: {completed.stdout.strip()!r}. "
            t"jj-stack requires jj {minimum_version} or later."
        )
    if version < _MINIMUM_JJ_VERSION:
        installed = ".".join(str(x) for x in version)
        raise CliError(
            f"jj {installed} is too old. "
            f"jj-stack requires jj {minimum_version} or later. "
            "Please upgrade jj."
        )
    _jj_version_verified = True


def _parse_jj_version(version_output: str) -> tuple[int, ...] | None:
    """Parse version tuple from `jj --version` output.

    Expected formats: ``"jj 0.45.1"`` or ``"jj 0.45.1-<build-hash>"``.
    Returns ``None`` if the output does not match the expected format.
    """

    parts = version_output.split()
    if len(parts) < 2 or parts[0] != "jj":
        return None
    version_str = parts[1].split("-")[0]
    try:
        return tuple(int(x) for x in version_str.split("."))
    except ValueError:
        return None


def validate_repo_path(repo: Path | None) -> None:
    if repo is None:
        return
    if not repo.exists():
        raise CliError(f"Repo path does not exist: {repo}")
    if not repo.is_dir():
        raise CliError(f"Repo path is not a directory: {repo}")
