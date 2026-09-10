"""jj's resolved configuration, read once per invocation."""

from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jj_stack.errors import CliError
from jj_stack.jj.cli_args import JjCliArgs


@dataclass(frozen=True, slots=True)
class JjSettings:
    """jj's effective configuration, defaults included, as nested TOML tables."""

    values: Mapping[str, object] = field(default_factory=dict)

    def table(self, *path: str) -> Mapping[str, object]:
        """Return the table at `path`, or an empty mapping when it is absent."""

        current: object = self.values
        for key in path:
            if not isinstance(current, Mapping):
                return {}
            current = current.get(key)
        return current if isinstance(current, Mapping) else {}

    def string(self, *path: str) -> str | None:
        """Return the string at `path`, or None when it is absent or not a string."""

        value = self.table(*path[:-1]).get(path[-1])
        return value if isinstance(value, str) else None


def read_jj_settings(*, cwd: Path, cli_args: JjCliArgs) -> JjSettings:
    """Run `jj config list --include-defaults` once and parse the whole listing."""

    command = [
        "jj",
        *cli_args.argv,
        "--ignore-working-copy",
        "config",
        "list",
        "--include-defaults",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=cwd,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CliError(f"Could not read jj config: {detail}")
    try:
        values = tomllib.loads(completed.stdout)
    except tomllib.TOMLDecodeError as error:
        raise CliError(f"Could not parse the jj config listing: {error}") from error
    return JjSettings(values)
