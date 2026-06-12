"""Runtime bootstrap helpers for CLI commands."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import jj_stack.ui as ui
from jj_stack.config import AppConfig, load_config
from jj_stack.errors import CliError
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.state.store import ReviewStateStore

_MINIMUM_JJ_VERSION = (0, 39, 0)
_MINIMUM_JJ_VERSION_STRING = "0.39.0"

APP_START = time.perf_counter()

time_output_active: bool = False


class _ElapsedFormatter(logging.Formatter):
    """Prepend the `--time-output` prefix when it's active."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if not time_output_active:
            return base
        from jj_stack import console

        elapsed = time.perf_counter() - APP_START
        return console.style_time_prefix(f"[{elapsed:0.6f}] ") + base


@dataclass(slots=True, frozen=True)
class RuntimeOptions:
    """Command-line options that influence bootstrap behavior."""

    cli_args: JjCliArgs
    debug: bool
    repository: Path | None


@dataclass(slots=True, frozen=True)
class CommandContext:
    """Typed runtime state shared by command handlers."""

    config: AppConfig
    jj_client: JjClient
    options: RuntimeOptions
    repo_root: Path
    state_store: ReviewStateStore


def bootstrap_context(
    *,
    repository: Path | None,
    cli_args: JjCliArgs,
    debug: bool,
) -> CommandContext:
    """Resolve the repository, load config, and initialize logging."""

    repository = _resolve_optional_path(repository)
    _validate_repository_path(repository)
    check_jj_version()
    repo_root = resolve_repo_root(repository or Path.cwd())
    jj_client = JjClient(repo_root, cli_args=cli_args)
    config = load_config(jj_client=jj_client)
    configure_logging(debug=debug, configured_level=config.logging.level)

    return CommandContext(
        config=config,
        jj_client=jj_client,
        options=RuntimeOptions(
            cli_args=cli_args,
            debug=debug,
            repository=repository,
        ),
        repo_root=repo_root,
        state_store=ReviewStateStore.for_repo(repo_root),
    )


def configure_logging(*, debug: bool, configured_level: str) -> None:
    """Apply process-wide logging defaults for the current command."""

    root_level = _resolve_logging_level(
        configured_level.upper(),
        original_value=configured_level,
    )
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
    logging.getLogger("httpxyz").setLevel(logging.WARNING)
    logging.getLogger("httpcorexyz").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _resolve_logging_level(level_name: str, *, original_value: str) -> int:
    level_names = logging.getLevelNamesMapping()
    if level_name not in level_names:
        valid_levels = ", ".join(sorted(level_names))
        raise CliError(
            f"Invalid logging level {original_value}. Expected one of: {valid_levels}"
        )
    return level_names[level_name]


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
    or if the installed version is older than the minimum.
    """

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

    version = _parse_jj_version(completed.stdout.strip())
    if version is None:
        raise CliError(
            t"Could not parse {ui.cmd('jj --version')} output: {completed.stdout.strip()!r}. "
            t"jj-stack requires jj {_MINIMUM_JJ_VERSION_STRING} or later."
        )
    if version < _MINIMUM_JJ_VERSION:
        installed = ".".join(str(x) for x in version)
        raise CliError(
            f"jj {installed} is too old. "
            f"jj-stack requires jj {_MINIMUM_JJ_VERSION_STRING} or later. "
            "Please upgrade jj."
        )


def _parse_jj_version(version_output: str) -> tuple[int, ...] | None:
    """Parse version tuple from `jj --version` output.

    Expected formats: ``"jj 0.39.0"`` or ``"jj 0.39.0-<build-hash>"``.
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


def _resolve_optional_path(raw_path: Path | str | None) -> Path | None:
    if raw_path is None:
        return None
    if isinstance(raw_path, Path):
        return raw_path.resolve()
    return Path(str(raw_path)).resolve()


def _validate_repository_path(repository: Path | None) -> None:
    if repository is None:
        return
    if not repository.exists():
        raise CliError(f"Repository path does not exist: {repository}")
    if not repository.is_dir():
        raise CliError(f"Repository path is not a directory: {repository}")
