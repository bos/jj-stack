"""Rendering for `jj-stack`'s semantic, Rich-styled `--help` output."""

from __future__ import annotations

import re
import textwrap
from argparse import SUPPRESS, ArgumentParser, _SubParsersAction
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from string.templatelib import Template
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
_INLINE_OPTION_PATTERN = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*(?:=[^\s]+)?)(?![\w-])")
_INLINE_METAVAR_PATTERN = re.compile(r"<[^>\s]+>")


@dataclass(frozen=True)
class HelpCommand:
    """One command entry in the grouped top-level help."""

    name: str
    summary: str
    hidden: bool = False


@dataclass(frozen=True)
class HelpSection:
    """A titled block of detailed guidance appended to one command's help."""

    title: str
    body: str


def normalized_help_text(content: ui.Message | str) -> str:
    return textwrap.dedent(ui.plain_text(content)).strip()


_ACTION_HELP_RENDERABLE_ATTRIBUTE = "_jj_stack_help_renderable"
_HELP_SECTIONS_ATTRIBUTE = "_jj_stack_help_sections"


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
        setattr(action, _ACTION_HELP_RENDERABLE_ATTRIBUTE, help)
    return action


def add_help_section(
    parser: ArgumentParser,
    *,
    title: str,
    body: ui.Message | str,
) -> None:
    """Append a detailed prose section to a command's help."""

    sections = (*_help_sections(parser), HelpSection(title, normalized_help_text(body)))
    setattr(parser, _HELP_SECTIONS_ATTRIBUTE, sections)


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

    option_actions = _top_level_option_actions(parser, include_hidden=include_hidden)
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
    for section in _help_sections(parser):
        console.output()
        console.output(_help_heading(f"{section.title}:"))
        _emit_help_paragraphs(section.body)


def render_all_in_one_markdown(
    parser: ArgumentParser,
    *,
    groups: Sequence[tuple[str, tuple[HelpCommand, ...]]],
    aliases: Mapping[str, tuple[str, ...]],
) -> str:
    """Render every command's semantic help as one Markdown and HTML fragment."""

    lines = ["<!-- Generated by jj-stack; do not edit. -->", ""]
    _append_markdown_overview(
        lines,
        parser,
        groups=groups,
        aliases=aliases,
        include_hidden=True,
    )
    lines.extend(("", "## Commands"))
    for title, entries in groups:
        command_parsers = _command_parsers(parser, entries, include_hidden=True)
        if command_parsers:
            lines.extend(("", f"### {title}"))
            for entry, child_parser in command_parsers:
                _append_markdown_command(
                    lines,
                    child_parser,
                    name=entry.name,
                    aliases=aliases.get(entry.name, ()),
                )
    return "\n".join(lines).rstrip() + "\n"


def _append_markdown_overview(
    lines: list[str],
    parser: ArgumentParser,
    *,
    groups: Sequence[tuple[str, tuple[HelpCommand, ...]]],
    aliases: Mapping[str, tuple[str, ...]],
    include_hidden: bool,
) -> None:
    lines.extend(
        (
            "## Usage",
            "",
            _usage_html(parser, _top_level_usage_message(include_hidden=include_hidden)),
        )
    )
    if parser.description:
        lines.extend(("", _description_html(parser.description)))

    for title, entries in groups:
        visible = tuple(entry for entry in entries if include_hidden or not entry.hidden)
        if visible:
            lines.extend(("", f"### {title}", "", _command_list_html(visible, aliases)))

    options = _top_level_option_actions(parser, include_hidden=include_hidden)
    if options:
        lines.extend(("", "## Global options", "", _action_list_html(options)))


def _append_markdown_command(
    lines: list[str],
    parser: ArgumentParser,
    *,
    name: str,
    aliases: Sequence[str],
) -> None:
    lines.extend(("", f"#### {name}"))
    if aliases:
        alias_html = ", ".join(
            f'<code class="cli-alias">{escape(alias)}</code>' for alias in aliases
        )
        lines.extend(("", f'<p class="cli-aliases"><strong>Aliases:</strong> {alias_html}</p>'))
    lines.extend(("", _usage_html(parser, _markdown_command_usage_message(parser))))
    if parser.description:
        description = parser.description
        if aliases:
            spelled = ", ".join(f"jj-stack {alias}" for alias in aliases)
            description = description.removesuffix(f"\n\nAlso spelled {spelled}.")
        lines.extend(("", _description_html(description)))

    positionals = _visible_actions(parser._positionals._group_actions)
    if positionals:
        lines.extend(("", "##### Positional arguments", "", _action_list_html(positionals)))
    options = tuple(
        action
        for action in _visible_actions(parser._optionals._group_actions)
        if not _is_common_option_action(action)
    )
    if options:
        lines.extend(("", "##### Options", "", _action_list_html(options)))
    for section in _help_sections(parser):
        lines.extend(("", f"##### {section.title}", "", _description_html(section.body)))


def _help_sections(parser: ArgumentParser) -> tuple[HelpSection, ...]:
    return getattr(parser, _HELP_SECTIONS_ATTRIBUTE, ())


def _command_parsers(
    parser: ArgumentParser,
    entries: Sequence[HelpCommand],
    *,
    include_hidden: bool,
) -> tuple[tuple[HelpCommand, ArgumentParser], ...]:
    subparsers = next(
        action for action in parser._actions if isinstance(action, _SubParsersAction)
    )
    return tuple(
        (entry, subparsers.choices[entry.name])
        for entry in entries
        if include_hidden or not entry.hidden
    )


def _command_list_html(
    entries: Sequence[HelpCommand],
    aliases: Mapping[str, tuple[str, ...]],
) -> str:
    rows = []
    for entry in entries:
        label = f'<code class="cli-command">{escape(entry.name)}</code>'
        entry_aliases = aliases.get(entry.name, ())
        alias_note = ""
        if entry_aliases:
            alias_labels = ", ".join(
                f'<code class="cli-alias">{escape(alias)}</code>' for alias in entry_aliases
            )
            alias_note = (
                f'<span class="cli-aliases"><strong>Aliases:</strong> {alias_labels}</span>'
            )
        rows.append(
            f'<div class="cli-reference-row"><dt>{label}</dt>'
            f"<dd>{_inline_help_html(normalized_help_text(entry.summary))}{alias_note}</dd></div>"
        )
    return '<dl class="cli-reference-list">\n' + "\n".join(rows) + "\n</dl>"


def _action_list_html(actions: Sequence[Any]) -> str:
    rows = [
        f'<div class="cli-reference-row"><dt>{_action_label_html(action)}</dt>'
        f"<dd>{_action_help_html(action)}</dd></div>"
        for action in actions
    ]
    return '<dl class="cli-reference-list">\n' + "\n".join(rows) + "\n</dl>"


def _action_label_html(action: Any) -> str:
    if not action.option_strings:
        label = escape(str(action.metavar or action.dest))
        return f'<code class="cli-argument"><var class="cli-metavar">{label}</var></code>'
    options = ", ".join(
        f'<span class="cli-option">{escape(option)}</span>' for option in action.option_strings
    )
    if action.nargs == 0:
        return f'<code class="cli-argument">{options}</code>'
    metavar = escape(str(action.metavar or action.dest.upper()))
    return f'<code class="cli-argument">{options} <var class="cli-metavar">{metavar}</var></code>'


def _action_help_html(action: Any) -> str:
    content = getattr(action, _ACTION_HELP_RENDERABLE_ATTRIBUTE, action.help or "")
    return _message_html(content)


def _description_html(text: str) -> str:
    blocks: list[str] = []
    bullets: list[str] = []
    for paragraph in _help_paragraphs(text):
        if paragraph.startswith("- "):
            bullets.append(f"<li>{_inline_help_html(paragraph[2:])}</li>")
            continue
        if bullets:
            blocks.append("<ul>\n" + "\n".join(bullets) + "\n</ul>")
            bullets = []
        blocks.append(f"<p>{_inline_help_html(paragraph)}</p>")
    if bullets:
        blocks.append("<ul>\n" + "\n".join(bullets) + "\n</ul>")
    return "\n".join(blocks)


def _message_html(content: ui.Message) -> str:
    if isinstance(content, tuple):
        return "".join(_message_html(part) for part in content)
    if isinstance(content, Template):
        return "".join(
            _inline_help_html(part)
            if isinstance(part, str)
            else _message_html(ui.resolve_interpolation(part))
            for part in content
        )
    if isinstance(content, ui.SemanticText):
        class_name = "cli-inline"
        if "revset" in content.labels or "metavar" in content.labels:
            class_name = "cli-metavar"
        elif "option" in content.labels:
            return _inline_option_html(content.text)
        elif "local_bookmarks" in content.labels or "remote_bookmarks" in content.labels:
            class_name = "cli-bookmark"
        elif "command" in content.labels and (
            _INLINE_OPTION_PATTERN.search(content.text)
            or _INLINE_METAVAR_PATTERN.search(content.text)
        ):
            return _inline_command_html(content.text)
        return f'<code class="{class_name}">{escape(content.text)}</code>'
    return _inline_help_html(str(content))


def _inline_option_html(text: str) -> str:
    match = re.fullmatch(
        r"(?P<option>--?[A-Za-z][\w-]*)(?:(?P<separator>=|\s+)(?P<value>.+))?",
        text,
    )
    if match is None or match.group("value") is None:
        return f'<code class="cli-inline cli-option">{escape(text)}</code>'
    return (
        '<code class="cli-inline cli-option-expression">'
        f'<span class="cli-option">{escape(match.group("option"))}</span>'
        f"{escape(match.group('separator'))}"
        f'<span class="cli-metavar">{escape(match.group("value"))}</span>'
        "</code>"
    )


def _inline_command_html(text: str) -> str:
    parts: list[str] = []
    last_index = 0
    tokens: list[tuple[int, int, str]] = []
    for match in _INLINE_OPTION_PATTERN.finditer(text):
        option = match.group(1)
        if "=" in option:
            option_name, value = option.split("=", 1)
            value_start = match.start(1) + len(option_name) + 1
            tokens.append((match.start(1), value_start - 1, "cli-option"))
            tokens.append((value_start, match.end(1), "cli-metavar"))
            continue
        tokens.append((match.start(1), match.end(1), "cli-option"))
        following = re.match(r"\s+(?P<value>(?!-)\S+)", text[match.end(1) :])
        if following is not None and not _INLINE_METAVAR_PATTERN.fullmatch(
            following.group("value")
        ):
            value_start = match.end(1) + following.start("value")
            value_end = match.end(1) + following.end("value")
            tokens.append((value_start, value_end, "cli-metavar"))
    tokens.extend(
        (match.start(), match.end(), "cli-metavar")
        for match in _INLINE_METAVAR_PATTERN.finditer(text)
    )
    for start, end, class_name in sorted(tokens):
        if start < last_index:
            continue
        parts.append(escape(text[last_index:start]))
        parts.append(f'<span class="{class_name}">{escape(text[start:end])}</span>')
        last_index = end
    parts.append(escape(text[last_index:]))
    return '<code class="cli-inline cli-command-snippet">' + "".join(parts) + "</code>"


def _inline_help_html(text: str) -> str:
    parts: list[str] = []
    last_index = 0
    for match in re.finditer(r"`([^`]+)`", text):
        parts.append(escape(text[last_index : match.start()]))
        parts.append(_message_html(_help_inline_code(match.group(1))))
        last_index = match.end()
    parts.append(escape(text[last_index:]))
    return "".join(parts)


def _usage_html(parser: ArgumentParser, usage: ui.Message | str) -> str:
    text = ui.plain_text(usage) if not isinstance(usage, str) else usage
    token_classes: dict[str, str] = {parser.prog: "cli-command"}
    for action in parser._actions:
        token_classes.update((option, "cli-option") for option in action.option_strings)
        if action.nargs != 0:
            if action.choices is not None and action.metavar is None:
                metavar = "{" + ",".join(str(choice) for choice in action.choices) + "}"
            else:
                metavar = action.metavar or (
                    action.dest.upper() if action.option_strings else action.dest
                )
            token_classes[str(metavar)] = "cli-metavar"
    token_classes["<command>"] = "cli-metavar"
    alternatives = "|".join(
        re.escape(token) for token in sorted(token_classes, key=len, reverse=True)
    )
    pattern = re.compile(rf"(?<![\w-])({alternatives})(?![\w-])")
    matches = tuple(pattern.finditer(text))
    parts: list[str] = []
    last_index = 0
    match_index = 0
    while match_index < len(matches):
        match = matches[match_index]
        parts.append(escape(text[last_index : match.start()]))
        token = match.group(1)
        next_match = matches[match_index + 1] if match_index + 1 < len(matches) else None
        if (
            token_classes[token] == "cli-option"
            and next_match is not None
            and token_classes[next_match.group(1)] == "cli-metavar"
            and text[match.end() : next_match.start()] == " "
        ):
            next_token = next_match.group(1)
            parts.append(
                '<span class="cli-argument">'
                f'<span class="cli-option">{escape(token)}</span> '
                f'<span class="cli-metavar">{escape(next_token)}</span>'
                "</span>"
            )
            last_index = next_match.end()
            match_index += 2
            continue
        parts.append(f'<span class="{token_classes[token]}">{escape(token)}</span>')
        last_index = match.end()
        match_index += 1
    parts.append(escape(text[last_index:]))
    return '<pre class="cli-synopsis"><code>' + "".join(parts) + "</code></pre>"


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


def _markdown_command_usage_message(parser: ArgumentParser) -> ui.Message | str:
    actions = tuple(action for action in parser._actions if not _is_common_option_action(action))
    groups = tuple(
        group
        for group in parser._mutually_exclusive_groups
        if any(action in actions for action in group._group_actions)
    )
    formatter: Any = parser._get_formatter()
    body = " ".join(formatter._format_usage(None, actions, groups, "").split())
    for action in actions:
        if not action.option_strings or action.option_strings[0].startswith("--"):
            continue
        long_option = next(
            (option for option in action.option_strings if option.startswith("--")), None
        )
        if long_option is not None:
            body = re.sub(
                rf"(?<![\w-]){re.escape(action.option_strings[0])}(?![\w-])",
                long_option,
                body,
            )
    if body.startswith(parser.prog):
        return (ui.cmd(parser.prog), body.removeprefix(parser.prog))
    return body


def _help_paragraphs(text: str) -> tuple[str, ...]:
    normalized = normalized_help_text(text)
    if not normalized:
        return ()
    return tuple(" ".join(paragraph.split()) for paragraph in re.split(r"\n\s*\n", normalized))


def _help_inline_code(text: str) -> ui.SemanticText:
    if text.startswith("jj-stack/"):
        return ui.bookmark(text)
    if text.startswith("@") or ("(" in text and text.endswith(")")):
        return ui.revset(text)
    if text.startswith("-"):
        return ui.option(text)
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
    content = getattr(action, _ACTION_HELP_RENDERABLE_ATTRIBUTE, None)
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
    visible_actions = _visible_actions(actions)
    if not visible_actions:
        return None
    return tuple(
        (
            _action_label_message(action),
            _action_help_body(action),
        )
        for action in visible_actions
    )


def _visible_actions(actions: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(action for action in actions if action.help is not SUPPRESS)


def _top_level_option_actions(
    parser: ArgumentParser,
    *,
    include_hidden: bool,
) -> tuple[Any, ...]:
    return tuple(
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
    )


def _emit_help_paragraphs(text: str) -> None:
    for index, paragraph in enumerate(_help_paragraphs(text)):
        if index:
            console.output()
        if paragraph.startswith("- "):
            console.output(ui.prefixed_line("- ", _help_rich_text(paragraph[2:])))
        else:
            console.output(_help_rich_text(paragraph))
