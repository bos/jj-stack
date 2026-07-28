"""Global `jj` CLI overrides, held below the client that forwards them."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JjCliArgs:
    """Global `jj` CLI overrides that flow to every jj invocation.

    Mirrors jj's own `--config NAME=VALUE` and `--config-file PATH` options so
    that a single value object carries the user's intent from the CLI down to
    every subprocess call. The argv is stored as one ordered tuple so the
    interleaving between `--config` and `--config-file` is preserved — jj
    applies later overrides on top of earlier ones, and a file listed after
    an inline value wins over it.
    """

    argv: tuple[str, ...] = ()

    def to_argv(self) -> tuple[str, ...]:
        return self.argv
