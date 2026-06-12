"""Rendering for `jj-stack`'s semantic, Rich-styled `--help` output."""

from __future__ import annotations

import re
import textwrap
from argparse import SUPPRESS, ArgumentParser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import jj_stack.console as console
import jj_stack.ui as ui

_TOP_LEVEL_HIDDEN_OPTION_STRINGS = frozenset(
    {"--repository", "--config", "--config-file", "--debug", "--time-output"}
)
_COMMON_OPTION_STRINGS = frozenset(
    {
        "-h",
        "--help",
        "--repository",
        "--config",
        "--config-file",
        "--debug",
        "--color",
        "--time-output",
    }
)


@dataclass(frozen=True)
class HelpCommand:
    """One command entry in the grouped top-level help."""

    name: str
    summary: str
    hidden: bool = False


def normalized_help_text(content: ui.Message | str) -> str:
    return textwrap.dedent(ui.plain_text(content)).strip()


_ACTION_HELP_RENDERABLES: dict[int, ui.Message] = {}


def add_help_argument(
    parser: Any,
    *name_or_flags: str,
    help: ui.Message | str,
    **kwargs: Any,
) -> Any:
    """Add an argument whose help text keeps its semantic styling when rendered."""

    action = parser.add_argument(*name_or_flags, **kwargs)
    action.help = normalized_help_text(help)
    if not isinstance(help, str):
        _ACTION_HELP_RENDERABLES[id(action)] = help
    return action


def emit_top_level_help(
    parser: ArgumentParser,
    *,
    groups: Sequence[tuple[str, tuple[HelpCommand, ...]]],
    aliases: Mapping[str, tuple[str, ...]],
    include_hidden: bool,
) -> None:
    console.output(
        ui.prefixed_line(
            _help_heading("Usage: "),
            _top_level_usage_message(include_hidden=include_hidden),
        )
    )

    if parser.description:
        console.output()
        _emit_help_paragraphs(parser.description)

    for title, entries in groups:
        visible_entries = [entry for entry in entries if include_hidden or not entry.hidden]
        if not visible_entries:
            continue
        console.output()
        _emit_help_table_section(
            title,
            tuple(
                (
                    ui.cmd(_command_label(entry, aliases, include_aliases=include_hidden)),
                    normalized_help_text(entry.summary),
                )
                for entry in visible_entries
            ),
        )

    if not include_hidden:
        console.output()
        console.output(
            t"Run {ui.cmd('jj-stack help --all')} to show advanced commands and options."
        )

    option_actions = [
        action
        for action in parser._actions
        if action.option_strings
        and action.help is not SUPPRESS
        and (
            include_hidden
            or not any(
                option in _TOP_LEVEL_HIDDEN_OPTION_STRINGS for option in action.option_strings
            )
        )
    ]
    option_rows = _action_rows(option_actions)
    if option_rows is not None:
        console.output()
        _emit_help_table_section("Options", option_rows)


def emit_command_help(parser: ArgumentParser) -> None:
    console.output(
        ui.prefixed_line(
            _help_heading("Usage: "),
            _command_usage_message(parser),
        )
    )

    if parser.description:
        console.output()
        _emit_help_paragraphs(parser.description)

    positional_rows = _action_rows(parser._positionals._group_actions)
    if positional_rows is not None:
        console.output()
        _emit_help_table_section(
            parser._positionals.title or "Positional Arguments",
            positional_rows,
        )

    option_actions = [
        action for action in parser._optionals._group_actions if action.help is not SUPPRESS
    ]
    command_option_rows = _action_rows(
        [action for action in option_actions if not _is_common_option_action(action)]
    )
    global_option_rows = _action_rows(
        [action for action in option_actions if _is_common_option_action(action)]
    )
    if command_option_rows is not None:
        console.output()
        title = "Command Options" if global_option_rows is not None else "Options"
        _emit_help_table_section(title, command_option_rows)
    if global_option_rows is not None:
        console.output()
        _emit_help_table_section("Global Options", global_option_rows)


def _command_label(
    entry: HelpCommand,
    aliases: Mapping[str, tuple[str, ...]],
    *,
    include_aliases: bool,
) -> str:
    entry_aliases = aliases.get(entry.name, ())
    if not include_aliases or not entry_aliases:
        return entry.name
    return ", ".join((entry.name, *entry_aliases))


def _top_level_usage_message(*, include_hidden: bool) -> ui.Message:
    if include_hidden:
        return (
            t"{ui.cmd('jj-stack')} [{ui.cmd('--help')}] "
            t"[{ui.cmd('--repository REPO')}] "
            t"[{ui.cmd('--config NAME=VALUE')}] [{ui.cmd('--config-file PATH')}] "
            t"[{ui.cmd('--debug')}] [{ui.cmd('--color WHEN')}] "
            t"[{ui.cmd('--time-output')}] [{ui.cmd('--version')}] "
            t"[{ui.cmd('<command>')} ...]"
        )
    return (
        t"{ui.cmd('jj-stack')} [{ui.cmd('--help')}] [{ui.cmd('--color WHEN')}] "
        t"[{ui.cmd('--version')}] [{ui.cmd('<command>')} ...]"
    )


def _command_usage_message(parser: ArgumentParser) -> ui.Message | str:
    body = " ".join(parser.format_usage().split())
    body = re.sub(r"^(?:[Uu]sage:\s*)+", "", body)
    body = re.sub(r"\[-h\]", "[--help]", body)
    if body.startswith(parser.prog):
        return (ui.cmd(parser.prog), body.removeprefix(parser.prog))
    return body


def _help_paragraphs(text: str) -> tuple[str, ...]:
    normalized = normalized_help_text(text)
    if not normalized:
        return ()
    return tuple(" ".join(paragraph.split()) for paragraph in re.split(r"\n\s*\n", normalized))


def _help_inline_code(text: str) -> ui.SemanticText:
    if text.startswith("review/"):
        return ui.bookmark(text)
    if text.startswith("@") or ("(" in text and text.endswith(")")):
        return ui.revset(text)
    return ui.cmd(text)


def _help_rich_text(text: str) -> ui.Message:
    parts: list[ui.Message] = []
    last_index = 0
    for match in re.finditer(r"`([^`]+)`", text):
        start, end = match.span()
        if start > last_index:
            parts.append(text[last_index:start])
        parts.append(_help_inline_code(match.group(1)))
        last_index = end
    if last_index == 0:
        return text
    if last_index < len(text):
        parts.append(text[last_index:])
    return tuple(parts)


def _help_heading(text: str) -> ui.SemanticText:
    return ui.semantic_text(text, "hint", "heading")


def _action_help_body(action: Any) -> ui.Message | str:
    content = _ACTION_HELP_RENDERABLES.get(id(action))
    if content is not None:
        return content
    return "\n\n".join(_help_paragraphs(action.help or ""))


def _action_label_message(action) -> ui.Message:
    if not action.option_strings:
        return ui.cmd(str(action.metavar or action.dest))
    label = ", ".join(action.option_strings)
    if action.nargs != 0:
        label = f"{label} {action.metavar or action.dest.upper()}"
    return ui.cmd(label)


def _help_table(
    rows: Sequence[tuple[ui.Message, ui.TableCell]],
) -> ui.DataTable:
    label_width = max(len(ui.plain_text(label)) for label, _ in rows) + 2
    return ui.DataTable(
        columns=(
            ui.TableColumn("", no_wrap=True, width=label_width),
            ui.TableColumn(""),
        ),
        rows=tuple(rows),
        box="",
        show_header=False,
    )


def _emit_help_table_section(title: str, rows: Sequence[tuple[ui.Message, ui.TableCell]]) -> None:
    console.output(_help_heading(f"{title}:"))
    console.output(_help_table(rows))


def _is_common_option_action(action: Any) -> bool:
    return bool(action.option_strings) and all(
        option in _COMMON_OPTION_STRINGS for option in action.option_strings
    )


def _action_rows(actions: Sequence[Any]) -> tuple[tuple[ui.Message, ui.TableCell], ...] | None:
    visible_actions = [action for action in actions if action.help is not SUPPRESS]
    if not visible_actions:
        return None
    return tuple(
        (
            _action_label_message(action),
            _action_help_body(action),
        )
        for action in visible_actions
    )


def _emit_help_paragraphs(text: str) -> None:
    for index, paragraph in enumerate(_help_paragraphs(text)):
        if index:
            console.output()
        console.output(_help_rich_text(paragraph))
