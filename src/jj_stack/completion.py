"""Shell completion generation for the standalone `jj-stack` executable."""

from __future__ import annotations

from argparse import SUPPRESS, Action, ArgumentParser, _SubParsersAction
from dataclasses import dataclass

_DIRECTORY_OPTION_DESTS = frozenset({"repository"})
_FILE_OPTION_DESTS = frozenset({"config"})


@dataclass(frozen=True)
class CompletionOption:
    """One CLI option plus any value-completion hint."""

    flags: tuple[str, ...]
    takes_value: bool
    value_kind: str = "none"


@dataclass(frozen=True)
class CompletionCommand:
    """One CLI command and its completion metadata."""

    name: str
    visible: bool
    options: tuple[CompletionOption, ...]
    positional_choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletionSpec:
    """Completion metadata derived from the argparse surface."""

    top_level_options: tuple[CompletionOption, ...]
    commands: tuple[CompletionCommand, ...]

    @property
    def all_command_names(self) -> tuple[str, ...]:
        return tuple(command.name for command in self.commands)

    @property
    def visible_command_names(self) -> tuple[str, ...]:
        return tuple(command.name for command in self.commands if command.visible)

    @property
    def value_option_flags(self) -> tuple[str, ...]:
        flags: list[str] = []
        for option in self.top_level_options:
            if option.takes_value:
                flags.extend(option.flags)
        for command in self.commands:
            for option in command.options:
                if option.takes_value:
                    flags.extend(option.flags)
        return tuple(dict.fromkeys(flags))


def emit_shell_completion(parser: ArgumentParser, shell: str) -> str:
    """Render a shell completion script for the requested shell."""

    spec = _build_completion_spec(parser)
    if shell == "bash":
        return _render_bash_completion(spec)
    if shell == "zsh":
        return _render_zsh_completion(spec)
    if shell == "fish":
        return _render_fish_completion(spec)
    raise ValueError(f"Unsupported shell: {shell}")


def _build_completion_spec(parser: ArgumentParser) -> CompletionSpec:
    subparsers_action = _find_subparsers_action(parser)
    commands: list[CompletionCommand] = []
    visible_by_parser_id: dict[int, bool] = {}
    for choice_action in subparsers_action._choices_actions:
        name = choice_action.dest
        command_parser = subparsers_action.choices[name]
        visible = choice_action.help != SUPPRESS
        visible_by_parser_id[id(command_parser)] = visible
        commands.append(
            CompletionCommand(
                name=name,
                visible=visible,
                options=_extract_options(command_parser),
                positional_choices=_extract_positional_choices(command_parser),
            )
        )
    canonical_names = {command.name for command in commands}
    for name, command_parser in subparsers_action.choices.items():
        if name in canonical_names:
            continue
        commands.append(
            CompletionCommand(
                name=name,
                visible=visible_by_parser_id.get(id(command_parser), True),
                options=_extract_options(command_parser),
                positional_choices=_extract_positional_choices(command_parser),
            )
        )
    return CompletionSpec(
        top_level_options=_extract_options(parser),
        commands=tuple(commands),
    )


def _find_subparsers_action(parser: ArgumentParser) -> _SubParsersAction[ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, _SubParsersAction):
            return action
    raise AssertionError("Expected parser to define subcommands.")


def _extract_options(parser: ArgumentParser) -> tuple[CompletionOption, ...]:
    options: list[CompletionOption] = []
    for action in parser._actions:
        if isinstance(action, _SubParsersAction) or not action.option_strings:
            continue
        if action.help == SUPPRESS:
            continue
        options.append(
            CompletionOption(
                flags=tuple(action.option_strings),
                takes_value=_option_takes_value(action),
                value_kind=_option_value_kind(action),
            )
        )
    return tuple(options)


def _extract_positional_choices(parser: ArgumentParser) -> tuple[str, ...]:
    for action in parser._actions:
        if action.option_strings or isinstance(action, _SubParsersAction):
            continue
        if action.choices is None:
            continue
        return tuple(str(choice) for choice in action.choices)
    return ()


def _option_takes_value(action: Action) -> bool:
    nargs = action.nargs
    if nargs == 0:
        return False
    if nargs is None:
        return True
    return bool(nargs)


def _option_value_kind(action: Action) -> str:
    if action.dest in _DIRECTORY_OPTION_DESTS:
        return "directory"
    if action.dest in _FILE_OPTION_DESTS:
        return "file"
    return "none"


def _render_bash_completion(spec: CompletionSpec) -> str:
    lines = [
        "_jj_stack_completion_visible_commands() {",
        f'    printf "%s" "{_join_words(spec.visible_command_names)}"',
        "}",
        "",
        "_jj_stack_completion_options() {",
        '    case "$1" in',
        f'        "") printf "%s" "{_join_words(_flags(spec.top_level_options))}" ;;',
    ]
    for command in spec.commands:
        lines.append(
            f'        {command.name}) printf "%s" "{_join_words(_flags(command.options))}" ;;'
        )
    lines.extend(
        [
            '        *) printf "%s" "" ;;',
            "    esac",
            "}",
            "",
            "_jj_stack_completion_positional_choices() {",
            '    case "$1" in',
        ]
    )
    for command in spec.commands:
        words = _join_words(command.positional_choices)
        lines.append(f'        {command.name}) printf "%s" "{words}" ;;')
    lines.extend(
        [
            '        *) printf "%s" "" ;;',
            "    esac",
            "}",
            "",
            "_jj_stack() {",
            "    local cur prev command word index options positional_choices",
            "    COMPREPLY=()",
            '    cur="${COMP_WORDS[COMP_CWORD]}"',
            '    prev=""',
            "    if (( COMP_CWORD > 0 )); then",
            '        prev="${COMP_WORDS[COMP_CWORD - 1]}"',
            "    fi",
            "",
            '    case "$prev" in',
        ]
    )
    for option in spec.value_option_flags:
        kind = _value_kind_for_flag(spec, option)
        if kind == "directory":
            lines.extend(
                [
                    f"        {option})",
                    '            COMPREPLY=( $(compgen -d -- "$cur") )',
                    "            return 0",
                    "            ;;",
                ]
            )
        elif kind == "file":
            lines.extend(
                [
                    f"        {option})",
                    '            COMPREPLY=( $(compgen -f -- "$cur") )',
                    "            return 0",
                    "            ;;",
                ]
            )
        else:
            lines.extend(
                [
                    f"        {option})",
                    "            return 0",
                    "            ;;",
                ]
            )
    lines.extend(
        [
            "    esac",
            "",
            '    command=""',
            "    index=1",
            "    while (( index < COMP_CWORD )); do",
            '        word="${COMP_WORDS[index]}"',
            '        case "$word" in',
        ]
    )
    for option in spec.value_option_flags:
        lines.extend(
            [
                f"            {option})",
                "                ((index += 2))",
                "                continue",
                "                ;;",
            ]
        )
    command_pattern = "|".join(spec.all_command_names)
    lines.extend(
        [
            f"            {command_pattern})",
            '                command="$word"',
            "                break",
            "                ;;",
            "        esac",
            "        ((index += 1))",
            "    done",
            "",
            '    if [[ -z "$command" ]]; then',
            '        options="$(_jj_stack_completion_options "") '
            '$(_jj_stack_completion_visible_commands)"',
            '        COMPREPLY=( $(compgen -W "$options" -- "$cur") )',
            "        return 0",
            "    fi",
            "",
            '    positional_choices="$(_jj_stack_completion_positional_choices "$command")"',
            '    if [[ -n "$positional_choices" && "$prev" == "$command" ]]; then',
            '        COMPREPLY=( $(compgen -W "$positional_choices" -- "$cur") )',
            "        return 0",
            "    fi",
            "",
            '    options="$(_jj_stack_completion_options "$command")"',
            '    COMPREPLY=( $(compgen -W "$options $positional_choices" -- "$cur") )',
            "}",
            "",
            "complete -F _jj_stack jj-stack",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_zsh_completion(spec: CompletionSpec) -> str:
    return (
        "#compdef jj-stack\n"
        "\n"
        "autoload -U +X bashcompinit || return 1\n"
        "bashcompinit || return 1\n"
        "\n"
        f"{_render_bash_completion(spec)}"
    )


def _render_fish_completion(spec: CompletionSpec) -> str:
    lines = ["complete -c jj-stack -f"]
    top_level_condition = "__fish_use_subcommand"
    for option in spec.top_level_options:
        lines.append(_fish_option_line(option, condition=top_level_condition))
    for command in spec.commands:
        if command.visible:
            lines.append(f"complete -c jj-stack -n '{top_level_condition}' -a '{command.name}'")
    for command in spec.commands:
        condition = f"__fish_seen_subcommand_from {command.name}"
        for option in command.options:
            lines.append(_fish_option_line(option, condition=condition))
        if command.positional_choices:
            choices = _join_words(command.positional_choices)
            lines.append(f"complete -c jj-stack -n '{condition}' -a '{choices}'")
    return "\n".join(lines) + "\n"


def _fish_option_line(option: CompletionOption, *, condition: str) -> str:
    pieces = ["complete", "-c", "jj-stack", "-n", f"'{condition}'"]
    short_flag = next((flag[1:] for flag in option.flags if flag.startswith("-")), None)
    long_flag = next((flag[2:] for flag in option.flags if flag.startswith("--")), None)
    if short_flag is not None and len(short_flag) == 1:
        pieces.extend(["-s", short_flag])
    if long_flag is not None:
        pieces.extend(["-l", long_flag])
    if option.takes_value:
        pieces.append("-r")
    if option.value_kind == "directory":
        pieces.extend(["-a", "'(__fish_complete_directories)'"])
    elif option.value_kind == "file":
        pieces.extend(["-a", "'(__fish_complete_path)'"])
    return " ".join(pieces)


def _flags(options: tuple[CompletionOption, ...]) -> tuple[str, ...]:
    flags: list[str] = []
    for option in options:
        flags.extend(option.flags)
    return tuple(flags)


def _join_words(words: tuple[str, ...]) -> str:
    return " ".join(words)


def _value_kind_for_flag(spec: CompletionSpec, flag: str) -> str:
    for option in spec.top_level_options:
        if flag in option.flags:
            return option.value_kind
    for command in spec.commands:
        for option in command.options:
            if flag in option.flags:
                return option.value_kind
    return "none"
