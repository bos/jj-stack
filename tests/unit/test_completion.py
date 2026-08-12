from __future__ import annotations

import pytest

from jj_stack.cli import build_parser, main
from jj_stack.completion import _build_completion_spec, emit_shell_completion


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


def test_completion_suggests_canonical_commands_but_accepts_typed_aliases() -> None:
    spec = _build_completion_spec(build_parser())

    assert {"submit", "view", "list"} <= set(spec.visible_command_names)
    for alias in ("sub", "status", "st", "v", "ls"):
        assert alias not in spec.visible_command_names
        assert alias in spec.all_command_names


@pytest.mark.parametrize(
    ("shell", "standalone_marker", "alias_marker"),
    [
        (
            "bash",
            "complete -F _jj_stack jj-stack",
            "complete -o nospace -o bashdefault -F _jj_stack_jj_dispatch jj",
        ),
        (
            "zsh",
            "complete -F _jj_stack jj-stack",
            "compdef _jj_stack_jj_dispatch jj",
        ),
        (
            "fish",
            "complete -c jj-stack -f",
            "complete -c jj -n '__jj_stack_jj_alias_at_root' -a 'submit'",
        ),
    ],
)
def test_alias_completion_routes_jj_and_keeps_standalone_completion(
    shell: str,
    standalone_marker: str,
    alias_marker: str,
) -> None:
    script = emit_shell_completion(build_parser(), shell, jj_alias="stack")

    assert standalone_marker in script
    assert alias_marker in script
    assert '"stack"' in script


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_command_prints_the_script_unaltered(shell: str, capsys) -> None:
    """The shell parses this output, so console formatting must not touch it.

    Printing it through the ordinary output path wrapped it to the console width, splitting a long
    `case` pattern mid-word and leaving a script no shell could parse.
    """

    expected = emit_shell_completion(build_parser(), shell, jj_alias="stack")

    exit_code = main(["completion", shell, "--jj-alias", "stack"])

    assert exit_code == 0
    assert capsys.readouterr().out == expected


def test_completion_rejects_an_alias_that_could_change_the_shell_script(capsys) -> None:
    exit_code = main(["completion", "bash", "--jj-alias", "stack;echo-bad"])

    assert exit_code == 5
    assert "A jj alias must start with a lowercase letter" in capsys.readouterr().err
