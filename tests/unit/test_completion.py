from __future__ import annotations

import pytest

from jj_stack.cli import build_parser, main
from jj_stack.completion import _build_completion_spec, emit_shell_completion


def test_completion_suggests_canonical_commands_but_accepts_typed_aliases() -> None:
    spec = _build_completion_spec(build_parser())

    assert {"submit", "view", "list"} <= set(spec.visible_command_names)
    repo_option = next(
        option for option in spec.top_level_options if "--repository" in option.flags
    )
    assert repo_option.value_kind == "directory"
    submit = next(command for command in spec.commands if command.name == "submit")
    submit_options = {flag: option for option in submit.options for flag in option.flags}
    # --edit shares its dest with the file-valued --resume-edit but takes no value itself.
    assert submit_options["--edit"].value_kind == "none"
    assert submit_options["--resume-edit"].value_kind == "file"
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
            "#compdef jj-stack",
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


def test_completion_command_prints_the_script_unaltered(capsys) -> None:
    """The shell parses this output, so console formatting must not touch it.

    Printing it through the ordinary output path wrapped it to the console width, splitting a long
    `case` pattern mid-word and leaving a script no shell could parse.
    """

    expected = emit_shell_completion(build_parser(), "bash", jj_alias="stack")

    exit_code = main(["completion", "bash", "--jj-alias", "stack"])

    assert exit_code == 0
    assert capsys.readouterr().out == expected


def test_completion_rejects_an_alias_that_could_change_the_shell_script(capsys) -> None:
    exit_code = main(["completion", "bash", "--jj-alias", "stack;echo-bad"])

    assert exit_code == 5
    assert "A jj alias must start with a lowercase letter" in capsys.readouterr().err
