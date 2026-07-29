from __future__ import annotations

import pytest

from jj_stack.cli import build_parser, main
from jj_stack.completion import emit_shell_completion


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        ("bash", "complete -F _jj_stack jj-stack"),
        ("zsh", "#compdef jj-stack"),
        ("fish", "complete -c jj-stack -f"),
    ],
)
def test_emit_shell_completion_smoke(shell: str, marker: str) -> None:
    script = emit_shell_completion(build_parser(), shell)

    assert marker in script
    assert "jj-stack" in script


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_command_prints_the_script_unaltered(shell: str, capsys) -> None:
    """The shell parses this output, so console formatting must not touch it.

    Printing it through the ordinary output path wrapped it to the console width, splitting a long
    `case` pattern mid-word and leaving a script no shell could parse.
    """

    expected = emit_shell_completion(build_parser(), shell)

    exit_code = main(["completion", shell])

    assert exit_code == 0
    assert capsys.readouterr().out == expected
