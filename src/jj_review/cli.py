"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import builtins
import io
import logging
import re
import subprocess
import sys
import textwrap
import time
from argparse import (
    SUPPRESS,
    ArgumentParser,
    HelpFormatter,
    Namespace,
    _SubParsersAction,
)
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jj_review import __version__, bootstrap, commands, console, ui
from jj_review.bootstrap import APP_START
from jj_review.completion import emit_shell_completion
from jj_review.console import ColorMode, RequestedColorMode, configured_console, rich_color_mode
from jj_review.errors import CliError, error_hint, error_message
from jj_review.jj import JjCliArgs

logger = logging.getLogger(__name__)
_COLOR_CHOICES: tuple[RequestedColorMode, ...] = ("always", "never", "debug", "auto")
_TOP_LEVEL_HELP_USAGE = "jj-review [--help] [--color WHEN] [--version] [<command> ...]"
_TOP_LEVEL_HELP_DESCRIPTION = """
`jj-review` lets you review a series of `jj` changes on GitHub as stacked pull requests.

Use it to submit and refresh changes for review, inspect pull request status, land ready
changes, list locally known stacks, and clean up after a review.
"""
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
_REORDERABLE_GLOBAL_FLAGS = frozenset({"--debug", "--time-output"})
_REORDERABLE_GLOBAL_OPTIONS_WITH_VALUES = frozenset({"--repository", "--color"})
_HELP_FLAGS = frozenset({"-h", "--help"})
_COMPLETION_HELP = "Print shell completion setup for bash, zsh, or fish"
_HELP_HELP = "Show help for this command or another command"
_COMPLETION_DESCRIPTION = """
Print the shell completion script for bash, zsh, or fish. This only prints
local shell setup text and does not inspect the repository or GitHub.
"""
_HELP_DESCRIPTION = """
Show top-level help or the detailed help for one command. Use `--all` to also
show the advanced repair commands and hidden global options.
"""


@dataclass(frozen=True)
class _HelpCommand:
    name: str
    summary: str
    hidden: bool = False


_TOP_LEVEL_HELP_GROUPS: tuple[tuple[str, tuple[_HelpCommand, ...]], ...] = (
    (
        "Core commands",
        (
            _HelpCommand("submit", commands.submit.HELP),
            _HelpCommand("status", commands.status.HELP),
            _HelpCommand("list", commands.list_.HELP),
            _HelpCommand("land", commands.land.HELP),
            _HelpCommand("close", commands.close.HELP),
        ),
    ),
    (
        "Support commands",
        (
            _HelpCommand("cleanup", commands.cleanup.HELP),
            _HelpCommand("import", commands.import_.HELP),
            _HelpCommand("abort", commands.abort.HELP),
            _HelpCommand("doctor", commands.doctor.HELP),
        ),
    ),
    (
        "Advanced repair",
        (
            _HelpCommand("restart", commands.restart.HELP, hidden=True),
            _HelpCommand("relink", commands.relink.HELP, hidden=True),
            _HelpCommand("unlink", commands.unlink.HELP, hidden=True),
        ),
    ),
    (
        "Configuration",
        (_HelpCommand("completion", _COMPLETION_HELP, hidden=True),),
    ),
    (
        "Help",
        (_HelpCommand("help", _HELP_HELP, hidden=True),),
    ),
)
_PULL_REQUEST_OPTION_STRINGS = ("-p", "--pull-request")
_COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "submit": ("sub",),
    "list": ("ls",),
    "status": ("st",),
}
_KNOWN_COMMANDS = frozenset(
    name
    for _, entries in _TOP_LEVEL_HELP_GROUPS
    for entry in entries
    for name in (entry.name, *_COMMAND_ALIASES.get(entry.name, ()))
)


class _TopLevelArgumentParser(ArgumentParser):
    """ArgumentParser with custom grouped help for the top-level CLI."""

    def format_usage(self) -> str:
        return f"usage: {_TOP_LEVEL_HELP_USAGE}\n"

    def error(self, message: str) -> None:
        raise _cli_parse_error(message)


class _TitleCaseHelpFormatter(HelpFormatter):
    """Help formatter that title-cases the usage heading."""

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix="Usage: ")


class _CommandArgumentParser(ArgumentParser):
    """ArgumentParser with title-cased built-in help headings."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _TitleCaseHelpFormatter)
        super().__init__(*args, **kwargs)
        self._positionals.title = "Positional Arguments"
        self._optionals.title = "Options"

    def error(self, message: str) -> None:
        raise _cli_parse_error(message)


def build_parser() -> ArgumentParser:
    """Build the top-level CLI parser and subcommands."""

    parser = _TopLevelArgumentParser(
        prog="jj-review",
        description=_normalized_help_text(_TOP_LEVEL_HELP_DESCRIPTION),
    )
    _add_common_options(parser, suppress_defaults=False)
    parser.set_defaults(command="status", handler=_default_status_handler)
    _normalize_help_action_text(parser)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        parser_class=_CommandArgumentParser,
    )
    submit_parser = _add_revision_command(
        subparsers,
        command="submit",
        aliases=_COMMAND_ALIASES["submit"],
        help_text=_normalized_help_text(commands.submit.HELP),
        description_text=commands.submit.__doc__ or "",
        handler=lambda args: commands.submit.submit(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            describe_with=args.describe_with,
            draft=args.draft,
            draft_all=args.draft_all,
            dry_run=args.dry_run,
            labels=args.labels,
            publish=args.publish,
            re_request=args.re_request,
            repository=args.repository,
            restart=args.restart,
            reviewers=args.reviewers,
            revset=args.revset,
            team_reviewers=args.team_reviewers,
            use_bookmarks=args.use_bookmarks,
        ),
        revset_help=(
            t"Revision to submit; defaults to {ui.revset('@-')} (the current stack head)"
        ),
    )
    _add_help_argument(
        submit_parser,
        "--dry-run",
        action="store_true",
        help="Print the submit plan without making any changes",
    )
    _add_help_argument(
        submit_parser,
        "-d",
        "--describe-with",
        metavar="HELPER",
        help="Delegate pull request and stack-comment text generation to HELPER",
    )
    submit_draft_mode = submit_parser.add_mutually_exclusive_group()
    _add_help_argument(
        submit_draft_mode,
        "--draft",
        action="store_true",
        help=(
            t"Create pull requests as drafts; use {ui.cmd('--draft=all')} to "
            t"return existing pull requests to draft"
        ),
    )
    submit_draft_mode.add_argument(
        "--draft-all",
        action="store_true",
        help=SUPPRESS,
    )
    submit_draft_mode.add_argument(
        "--publish",
        action="store_true",
        help="Mark existing draft pull requests ready for review on submit",
    )
    _add_help_argument(
        submit_parser,
        "--label",
        dest="labels",
        action="append",
        help="Apply GitHub labels to submitted pull requests",
    )
    _add_help_argument(
        submit_parser,
        "--reviewers",
        dest="reviewers",
        action="append",
        metavar="USERS",
        help="Request reviews from GitHub users on submitted pull requests",
    )
    _add_help_argument(
        submit_parser,
        "--team-reviewers",
        dest="team_reviewers",
        action="append",
        metavar="TEAMS",
        help="Ask for reviews from GitHub teams on submitted pull requests",
    )
    _add_help_argument(
        submit_parser,
        "--use-bookmarks",
        dest="use_bookmarks",
        metavar="BOOKMARKS",
        action="append",
        help=(
            "Prefer existing bookmark names or globs for selected changes. "
            "Reused bookmarks stay during cleanup unless cleanup_user_bookmarks "
            "is true"
        ),
    )
    _add_help_argument(
        submit_parser,
        "--re-request",
        action="store_true",
        help=(
            "Request review again from users whose latest review on an existing "
            "pull request approved it or requested changes"
        ),
    )
    _add_help_argument(
        submit_parser,
        "--restart",
        action="store_true",
        help="Forget previous PR tracking for selected changes and create fresh PRs",
    )
    status_parser = _add_revision_command(
        subparsers,
        command="status",
        aliases=_COMMAND_ALIASES["status"],
        help_text=_normalized_help_text(commands.status.HELP),
        description_text=commands.status.__doc__ or "",
        handler=lambda args: commands.status.status(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            fetch=args.fetch,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
            selectors=getattr(args, "status_selectors", None),
            verbose=args.verbose,
        ),
        revset_help=(
            "Revsets to inspect; can be mixed with repeated --pull-request selectors; "
            "defaults to the current stack"
        ),
        revset_nargs="*",
    )
    _add_help_argument(
        status_parser,
        *_PULL_REQUEST_OPTION_STRINGS,
        metavar="PR",
        action="append",
        help="Inspect the stack for this PR number or URL; repeat to inspect several stacks",
    )
    status_parser.add_argument(
        "-f",
        "--fetch",
        action="store_true",
        help="Fetch first so status uses current remote branch locations",
    )
    status_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Expand submitted and unsubmitted summary sections; keep native jj log lines",
    )
    list_parser = subparsers.add_parser(
        "list",
        aliases=list(_COMMAND_ALIASES["list"]),
        help=_normalized_help_text(commands.list_.HELP),
        description=_normalized_help_text(commands.list_.__doc__ or ""),
    )
    _add_common_options(list_parser)
    _normalize_help_action_text(list_parser)
    list_parser.add_argument(
        "-f",
        "--fetch",
        action="store_true",
        help="Fetch first so list uses current remote branch locations",
    )
    list_parser.set_defaults(
        handler=lambda args: commands.list_.list_(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            fetch=args.fetch,
            repository=args.repository,
        )
    )
    _add_relink_parser(
        subparsers,
        command="relink",
        help_text=_normalized_help_text(commands.relink.HELP),
        description_text=commands.relink.__doc__ or "",
        handler=lambda args: commands.relink.relink(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
    )
    restart_parser = _add_revision_command(
        subparsers,
        command="restart",
        help_text=_normalized_help_text(commands.restart.HELP),
        description_text=commands.restart.__doc__ or "",
        handler=lambda args: commands.restart.restart(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            dry_run=args.dry_run,
            repository=args.repository,
            revset=args.revset,
        ),
        revset_nargs=None,
        revset_help="Stack head to prepare for fresh pull requests",
    )
    restart_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be reset without changing tracking data",
    )
    _add_revision_command(
        subparsers,
        command="unlink",
        help_text=_normalized_help_text(commands.unlink.HELP),
        description_text=commands.unlink.__doc__ or "",
        handler=lambda args: commands.unlink.unlink(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            repository=args.repository,
            revset=args.revset,
        ),
        revset_nargs=None,
        revset_help="Revision to unlink",
    )
    land_parser = _add_revision_command(
        subparsers,
        command="land",
        help_text=_normalized_help_text(commands.land.HELP),
        description_text=commands.land.__doc__ or "",
        handler=lambda args: commands.land.land(
            dry_run=args.dry_run,
            bypass_readiness=args.bypass_readiness,
            cli_args=_global_cli_args(args),
            debug=args.debug,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
            skip_cleanup=args.skip_cleanup,
        ),
        revset_help=(
            t"Revision to land; defaults to {ui.revset('@-')} (the current stack head); "
            t"cannot be combined with {ui.cmd('--pull-request')}"
        ),
    )
    land_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the landing plan without making any changes",
    )
    _add_help_argument(
        land_parser,
        *_PULL_REQUEST_OPTION_STRINGS,
        metavar="PR",
        help="Select the local change linked to this pull request number or URL",
    )
    land_parser.add_argument(
        "--bypass-readiness",
        action="store_true",
        help=("Skip draft and review-decision checks while keeping normal safety checks"),
    )
    land_parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Keep landed local review bookmarks instead of forgetting them",
    )
    close_parser = _add_revision_command(
        subparsers,
        command="close",
        help_text=_normalized_help_text(commands.close.HELP),
        description_text=commands.close.__doc__ or "",
        handler=lambda args: commands.close.close(
            dry_run=args.dry_run,
            cleanup=args.cleanup,
            cli_args=_global_cli_args(args),
            debug=args.debug,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
        revset_help=(
            t"Revision to close; defaults to {ui.revset('@-')} (the current stack head); "
            t"cannot be combined with {ui.cmd('--pull-request')}"
        ),
    )
    close_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the close plan without making any changes",
    )
    close_parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete jj-review-managed branches, bookmarks, and tracking data",
    )
    _add_help_argument(
        close_parser,
        *_PULL_REQUEST_OPTION_STRINGS,
        metavar="PR",
        help=(
            "Select a stack by PR number or URL; with --cleanup, this can also retire "
            "an orphaned PR shown by list"
        ),
    )
    _add_import_parser(
        subparsers,
        command="import",
        help_text=_normalized_help_text(commands.import_.HELP),
        description_text=commands.import_.__doc__ or "",
        handler=lambda args: commands.import_.import_(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            fetch=args.fetch,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
    )

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help=_normalized_help_text(commands.cleanup.HELP),
        description=_normalized_help_text(commands.cleanup.__doc__ or ""),
    )
    _add_common_options(cleanup_parser)
    _normalize_help_action_text(cleanup_parser)
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cleanup actions without making any changes",
    )
    _add_help_argument(
        cleanup_parser,
        "--rebase",
        nargs="?",
        const="@-",
        metavar="REVSET",
        help=(
            t"Rebase the selected stack above changes already merged on GitHub; "
            t"defaults to {ui.revset('@-')} when passed without an explicit revset"
        ),
    )
    cleanup_parser.set_defaults(
        handler=lambda args: commands.cleanup.cleanup(
            dry_run=args.dry_run,
            cli_args=_global_cli_args(args),
            debug=args.debug,
            repository=args.repository,
            rebase_revset=args.rebase,
        )
    )

    abort_parser = subparsers.add_parser(
        "abort",
        help=_normalized_help_text(commands.abort.HELP),
        description=_normalized_help_text(commands.abort.__doc__ or ""),
    )
    _add_common_options(abort_parser)
    _normalize_help_action_text(abort_parser)
    abort_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be undone without changing anything",
    )
    abort_parser.set_defaults(
        handler=lambda args: commands.abort.abort(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            dry_run=args.dry_run,
            repository=args.repository,
        )
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help=_normalized_help_text(commands.doctor.HELP),
        description=_normalized_help_text(commands.doctor.__doc__ or ""),
    )
    _add_common_options(doctor_parser)
    _normalize_help_action_text(doctor_parser)
    doctor_parser.set_defaults(
        handler=lambda args: commands.doctor.doctor(
            cli_args=_global_cli_args(args),
            debug=args.debug,
            repository=args.repository,
        )
    )

    completion_parser = subparsers.add_parser(
        "completion",
        help=_COMPLETION_HELP,
        description=_normalized_help_text(_COMPLETION_DESCRIPTION),
    )
    _normalize_help_action_text(completion_parser)
    completion_parser.add_argument(
        "shell",
        choices=("bash", "zsh", "fish"),
        help="Shell to generate completion support for",
    )
    completion_parser.set_defaults(handler=_completion_handler)
    help_parser = subparsers.add_parser(
        "help",
        help=SUPPRESS,
        description=_normalized_help_text(_HELP_DESCRIPTION),
    )
    _normalize_help_action_text(help_parser)
    _add_help_argument(
        help_parser,
        "--all",
        action="store_true",
        help="Include advanced repair and shell integration commands",
    )
    help_parser.add_argument(
        "command",
        nargs="?",
        help="Command to describe",
    )
    help_parser.set_defaults(handler=_help_handler)
    return parser


def _format_option_label(action) -> str:
    if action.nargs == 0:
        return ", ".join(action.option_strings)
    metavar = action.metavar or action.dest.upper()
    return f"{', '.join(action.option_strings)} {metavar}"


def _normalized_help_text(content: ui.Message | str) -> str:
    return textwrap.dedent(ui.plain_text(content)).strip()


def _help_paragraphs(text: str) -> tuple[str, ...]:
    normalized = _normalized_help_text(text)
    if not normalized:
        return ()
    return tuple(" ".join(paragraph.split()) for paragraph in re.split(r"\n\s*\n", normalized))


def _help_inline_code(text: str) -> ui.SemanticText:
    if text.startswith("review/"):
        return ui.bookmark(text)
    if text.startswith("@") or ("(" in text and text.endswith(")")):
        return ui.revset(text)
    return ui.cmd(text)


def _help_rich_text(text: str) -> ui.Message | str:
    parts: list[object] = []
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


_ACTION_HELP_RENDERABLES: dict[int, ui.Message] = {}


def _add_help_argument(
    parser: Any,
    *name_or_flags: str,
    help: ui.Message | str,
    **kwargs: Any,
) -> Any:
    action = parser.add_argument(*name_or_flags, **kwargs)
    action.help = _normalized_help_text(help)
    if not isinstance(help, str):
        _ACTION_HELP_RENDERABLES[id(action)] = help
    return action


def _action_help_body(action: Any) -> ui.Message | str:
    content = _ACTION_HELP_RENDERABLES.get(id(action))
    if content is not None:
        return content
    return "\n\n".join(_help_paragraphs(action.help or ""))


def _top_level_usage_message(*, include_hidden: bool) -> ui.Message:
    if include_hidden:
        return (
            t"{ui.cmd('jj-review')} [{ui.cmd('--help')}] "
            t"[{ui.cmd('--repository REPO')}] "
            t"[{ui.cmd('--config NAME=VALUE')}] [{ui.cmd('--config-file PATH')}] "
            t"[{ui.cmd('--debug')}] [{ui.cmd('--color WHEN')}] "
            t"[{ui.cmd('--time-output')}] [{ui.cmd('--version')}] "
            t"[{ui.cmd('<command>')} ...]"
        )
    return (
        t"{ui.cmd('jj-review')} [{ui.cmd('--help')}] [{ui.cmd('--color WHEN')}] "
        t"[{ui.cmd('--version')}] [{ui.cmd('<command>')} ...]"
    )


def _usage_body_from_parser(parser: ArgumentParser) -> str:
    usage = " ".join(parser.format_usage().split())
    usage = re.sub(r"^(?:[Uu]sage:\s*)+", "", usage)
    usage = re.sub(r"\[-h\]", "[--help]", usage)
    return usage


def _command_usage_message(parser: ArgumentParser) -> ui.Message | str:
    body = _usage_body_from_parser(parser)
    if body.startswith(parser.prog):
        return (ui.cmd(parser.prog), body.removeprefix(parser.prog))
    return body


def _action_label_message(action) -> ui.Message:
    if action.option_strings:
        return ui.cmd(_format_option_label(action))
    return ui.cmd(str(action.metavar or action.dest))


def _help_table(
    rows: Sequence[tuple[Any, Any]],
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


def _action_rows_for_actions(
    actions: Sequence[Any],
) -> tuple[tuple[Any, Any], ...]:
    return tuple(
        (
            _action_label_message(action),
            _action_help_body(action),
        )
        for action in actions
    )


def _emit_help_table_section(title: str, rows: Sequence[tuple[Any, Any]]) -> None:
    console.output(_help_heading(f"{title}:"))
    console.output(_help_table(rows))


def _is_common_option_action(action: Any) -> bool:
    return bool(action.option_strings) and all(
        option in _COMMON_OPTION_STRINGS for option in action.option_strings
    )


def _action_rows(actions: Sequence[Any]) -> tuple[tuple[Any, Any], ...] | None:
    visible_actions = [action for action in actions if action.help is not SUPPRESS]
    if not visible_actions:
        return None
    return _action_rows_for_actions(visible_actions)


def _emit_help_paragraphs(text: str) -> None:
    for index, paragraph in enumerate(_help_paragraphs(text)):
        if index:
            console.output()
        console.output(_help_rich_text(paragraph))


def _emit_top_level_help(parser: ArgumentParser, *, include_hidden: bool) -> None:
    console.output(
        ui.prefixed_line(
            _help_heading("Usage: "),
            _top_level_usage_message(include_hidden=include_hidden),
        )
    )

    if parser.description:
        console.output()
        _emit_help_paragraphs(parser.description)

    for title, entries in _TOP_LEVEL_HELP_GROUPS:
        visible_entries = [entry for entry in entries if include_hidden or not entry.hidden]
        if not visible_entries:
            continue
        console.output()
        _emit_help_table_section(
            title,
            tuple(
                (
                    ui.cmd(_top_level_command_label(entry, include_aliases=include_hidden)),
                    _normalized_help_text(entry.summary),
                )
                for entry in visible_entries
            ),
        )

    if not include_hidden:
        console.output()
        console.output(
            t"Run {ui.cmd('jj-review help --all')} to show advanced commands and options."
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


def _top_level_command_label(entry: _HelpCommand, *, include_aliases: bool) -> str:
    aliases = _COMMAND_ALIASES.get(entry.name, ())
    if not include_aliases or not aliases:
        return entry.name
    return ", ".join((entry.name, *aliases))


def _help_handler(args: Namespace) -> int:
    parser = build_parser()
    if args.command is None:
        _emit_top_level_help(parser, include_hidden=args.all)
        return 0

    command_parser = _find_subcommand_parser(parser, args.command)
    if command_parser is None:
        raise _unknown_command_error(args.command)

    console.output(
        ui.prefixed_line(
            _help_heading("Usage: "),
            _command_usage_message(command_parser),
        )
    )

    if command_parser.description:
        console.output()
        _emit_help_paragraphs(command_parser.description)

    positional_rows = _action_rows(command_parser._positionals._group_actions)
    if positional_rows is not None:
        console.output()
        _emit_help_table_section(
            command_parser._positionals.title or "Positional Arguments",
            positional_rows,
        )

    option_actions = [
        action
        for action in command_parser._optionals._group_actions
        if action.help is not SUPPRESS
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
    return 0


def _find_subcommand_parser(
    parser: ArgumentParser,
    command_name: str,
) -> ArgumentParser | None:
    for action in parser._actions:
        if not isinstance(action, _SubParsersAction):
            continue
        parser_choice = action.choices.get(command_name)
        if parser_choice is not None:
            return parser_choice
    return None


def _print_cli_error(error: CliError) -> None:
    message = error_message(error)
    if str(error).startswith("Error:"):
        console.error(message, soft_wrap=True)
    else:
        console.error(("Error: ", message), soft_wrap=True)
    hint = error_hint(error)
    if hint is not None:
        console.stderr_output(
            (ui.semantic_text("Hint: ", "hint", "heading"), hint),
            soft_wrap=True,
        )


def _print_early_cli_error(
    error: CliError,
    *,
    cli_args: JjCliArgs,
    normalized_argv: Sequence[str],
) -> None:
    requested_color_mode = _color_arg_from_argv(normalized_argv)
    with configured_console(
        cli_args=cli_args,
        color_mode=rich_color_mode(requested_color_mode),
        repository=None,
        requested_color_mode=requested_color_mode,
        time_output=False,
    ):
        _print_cli_error(error)


def _cli_parse_error(message: str) -> CliError:
    message = message.strip()
    invalid_choice = re.match(
        r"argument (?P<argument>[^:]+): invalid choice: '(?P<value>[^']+)'(?: .*)?$",
        message,
    )
    if invalid_choice is not None and invalid_choice.group("argument") == "command":
        return _unknown_command_error(invalid_choice.group("value"))
    if message and not message.endswith("."):
        message = f"{message}."
    if message:
        message = f"{message[0].upper()}{message[1:]}"
    return CliError(message)


def _unknown_command_error(command_name: str) -> CliError:
    return CliError(
        t"Unknown command {ui.cmd(command_name)}.",
        hint=t"Run {ui.cmd('jj-review help')} to list commands.",
    )


def _color_arg_from_argv(argv: Sequence[str]) -> RequestedColorMode | None:
    for index, arg in enumerate(argv):
        if arg.startswith("--color="):
            value = arg.partition("=")[2]
        elif arg == "--color" and index + 1 < len(argv):
            value = argv[index + 1]
        else:
            continue
        if value in _COLOR_CHOICES:
            return cast(RequestedColorMode, value)
        return None
    return None


def _load_configured_jj_color(
    *,
    repository: Path | None,
    cli_args: JjCliArgs,
) -> RequestedColorMode | None:
    """Read `ui.color` from `jj` config without requiring repository bootstrap."""

    cwd = (
        repository
        if repository is not None and repository.exists() and repository.is_dir()
        else Path.cwd()
    )
    try:
        completed = subprocess.run(
            ["jj", *cli_args.to_argv(), "config", "get", "ui.color"],
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None

    if completed.returncode != 0:
        return None

    configured = completed.stdout.strip()
    if configured in _COLOR_CHOICES:
        return cast(RequestedColorMode, configured)
    return None


def _resolve_rich_color_mode(
    *,
    cli_color: RequestedColorMode | None,
    cli_args: JjCliArgs,
    repository: Path | None,
) -> tuple[RequestedColorMode | None, ColorMode]:
    raw_color = cli_color
    if raw_color is None:
        raw_color = _load_configured_jj_color(repository=repository, cli_args=cli_args)
    return raw_color, rich_color_mode(raw_color)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    cli_args = JjCliArgs()
    normalized_argv = list(sys.argv[1:] if argv is None else argv)
    status_args: _ParsedStatusCommandArgs | None = None
    try:
        cli_args, stripped_argv = _extract_config_overrides(normalized_argv)
        normalized_argv = _normalize_cli_args(stripped_argv)
        status_args = _parse_status_command_args(normalized_argv)
        if status_args is not None:
            normalized_argv = list(status_args.argv)
        args = parser.parse_args(normalized_argv)
    except CliError as error:
        _print_early_cli_error(
            error,
            cli_args=cli_args,
            normalized_argv=normalized_argv,
        )
        return error.exit_code
    args.cli_args = cli_args
    args.normalized_argv = tuple(normalized_argv)
    if args.command in {"status", "st"}:
        args.status_selectors = () if status_args is None else status_args.selectors
    _, effective_rich_color_mode = _resolve_rich_color_mode(
        cli_color=args.color,
        cli_args=cli_args,
        repository=args.repository,
    )
    with configured_console(
        cli_args=cli_args,
        color_mode=effective_rich_color_mode,
        repository=args.repository,
        requested_color_mode=args.color,
        time_output=args.time_output,
    ):
        with _time_output(enabled=args.time_output):
            handler = args.handler
            try:
                return handler(args)
            except CliError as error:
                _print_cli_error(error)
                return error.exit_code
            except KeyboardInterrupt:
                console.stderr_output("Interrupted.")
                return 130


def _default_status_handler(args: Namespace) -> int:
    """Run bare `jj-review` as the default `status` command."""

    return commands.status.status(
        cli_args=_global_cli_args(args),
        debug=args.debug,
        fetch=False,
        pull_request=None,
        repository=args.repository,
        revset=None,
        selectors=(),
        verbose=False,
    )


@dataclass(frozen=True)
class _ParsedStatusCommandArgs:
    argv: tuple[str, ...]
    selectors: tuple[commands.status.StatusSelector, ...]


def _parse_status_command_args(argv: Sequence[str]) -> _ParsedStatusCommandArgs | None:
    """Rewrite `status` argv and preserve explicit selector order."""

    command_index = _find_subcommand_index(argv)
    if command_index is None or argv[command_index] not in {"status", "st"}:
        return None

    prefix = list(argv[: command_index + 1])
    command_argv = argv[command_index + 1 :]
    options: list[str] = []
    revsets: list[str] = []
    selectors: list[commands.status.StatusSelector] = []
    index = 0
    while index < len(command_argv):
        arg = command_argv[index]
        if arg == "--":
            trailing_revsets = command_argv[index + 1 :]
            revsets.extend(trailing_revsets)
            selectors.extend(
                commands.status.StatusSelector(kind="revset", value=value)
                for value in trailing_revsets
            )
            break
        if arg in {*_PULL_REQUEST_OPTION_STRINGS, "--repository", "--color"}:
            if index + 1 >= len(command_argv):
                options.extend(command_argv[index:])
                break
            value = command_argv[index + 1]
            options.extend((arg, value))
            if arg in _PULL_REQUEST_OPTION_STRINGS:
                selectors.append(
                    commands.status.StatusSelector(
                        kind="pull_request",
                        value=value,
                    )
                )
            index += 2
            continue
        if (
            arg.startswith("--pull-request=")
            or (arg.startswith("-p") and len(arg) > 2)
            or arg.startswith("--repository=")
            or arg.startswith("--color=")
        ):
            options.append(arg)
            if arg.startswith("--pull-request="):
                value = arg.partition("=")[2]
            elif arg.startswith("-p") and len(arg) > 2:
                value = arg[2:].removeprefix("=")
            else:
                value = None
            if value is not None:
                selectors.append(
                    commands.status.StatusSelector(
                        kind="pull_request",
                        value=value,
                    )
                )
            index += 1
            continue
        if arg in _STATUS_SELECTOR_FLAGS or _is_grouped_status_flag(arg):
            options.append(arg)
            index += 1
            continue
        if arg.startswith("-"):
            options.append(arg)
            index += 1
            continue
        revsets.append(arg)
        selectors.append(commands.status.StatusSelector(kind="revset", value=arg))
        index += 1
    normalized = [*prefix, *options]
    if revsets:
        normalized.extend(["--", *revsets])
    return _ParsedStatusCommandArgs(argv=tuple(normalized), selectors=tuple(selectors))


_STATUS_SELECTOR_FLAGS = frozenset(
    {"-f", "--fetch", "-v", "--verbose", "-h", "--help", "--debug", "--time-output"}
)


def _find_subcommand_index(argv: Sequence[str]) -> int | None:
    """Return the index of the top-level subcommand, if present."""

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return None
        if arg in {"--version", *_HELP_FLAGS, *_REORDERABLE_GLOBAL_FLAGS}:
            index += 1
            continue
        if arg in _REORDERABLE_GLOBAL_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(arg.startswith(f"{opt}=") for opt in _REORDERABLE_GLOBAL_OPTIONS_WITH_VALUES):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return index
    return None


def _is_grouped_status_flag(arg: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and set(arg[1:]) <= {"f", "v", "h"}


def _add_revision_command[SubparserT: ArgumentParser](
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    aliases: Sequence[str] = (),
    help_text: str,
    description_text: str,
    handler,
    revset_nargs: str | int | None = "?",
    revset_help: ui.Message | str = "Revision to operate on",
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        aliases=list(aliases),
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    _add_help_argument(parser, "revset", nargs=revset_nargs, help=revset_help)
    parser.set_defaults(handler=handler)
    return parser


def _add_relink_parser[SubparserT: ArgumentParser](
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler,
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    _add_help_argument(parser, "pull_request", help="Pull request number or URL")
    _add_help_argument(parser, "revset", help="Revision to reassociate with the pull request")
    parser.set_defaults(handler=handler)
    return parser


def _add_import_parser[SubparserT: ArgumentParser](
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler,
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    selector = parser.add_mutually_exclusive_group(required=False)
    _add_help_argument(
        selector,
        *_PULL_REQUEST_OPTION_STRINGS,
        metavar="PR",
        help="Pull request number or URL",
    )
    _add_help_argument(
        selector,
        "--revset",
        help="Explicit revset whose exact stack should be imported",
    )
    _add_help_argument(
        parser,
        "--fetch",
        action="store_true",
        help=(
            t"Refresh the selected stack's remote bookmark state and, for "
            t"{ui.cmd('--pull-request')}, fetch only the branches needed to import "
            t"that stack"
        ),
    )
    parser.set_defaults(handler=handler)
    return parser


def _add_common_options(
    parser: ArgumentParser,
    *,
    suppress_defaults: bool = True,
) -> None:
    parser.add_argument(
        "--repository",
        type=Path,
        metavar="REPO",
        default=SUPPRESS if suppress_defaults else None,
        help="Workspace path to operate on; defaults to the current directory",
    )
    # --config and --config-file are extracted from argv by
    # `_extract_config_overrides` before argparse runs, because argparse
    # subparsers create fresh namespaces and would otherwise clobber any
    # overrides passed before the subcommand. These registrations exist only
    # so the flags appear in --help output.
    parser.add_argument(
        "--config",
        action="store",
        default=SUPPRESS,
        dest=SUPPRESS,
        metavar="NAME=VALUE",
        help=(
            "Additional jj config option as a TOML dotted-key assignment (e.g. ui.color=always)"
        ),
    )
    parser.add_argument(
        "--config-file",
        action="store",
        default=SUPPRESS,
        dest=SUPPRESS,
        metavar="PATH",
        help="Additional config files",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=SUPPRESS if suppress_defaults else False,
        help="Enable debug logging",
    )
    parser.add_argument(
        "--color",
        choices=_COLOR_CHOICES,
        default=SUPPRESS if suppress_defaults else None,
        metavar="WHEN",
        help="When to colorize output; possible values: always, never, debug, auto",
    )
    parser.add_argument(
        "--time-output",
        action="store_true",
        default=SUPPRESS if suppress_defaults else False,
        help="Prefix each printed line with elapsed seconds since process start",
    )


def _normalize_help_action_text(parser: ArgumentParser) -> None:
    for action in parser._actions:
        if action.option_strings == ["-h", "--help"]:
            action.help = "Show help"
            return


_CONFIG_OVERRIDE_FLAGS = frozenset({"--config", "--config-file"})


def _extract_config_overrides(argv: Sequence[str]) -> tuple[JjCliArgs, list[str]]:
    """Pull ``--config`` / ``--config-file`` out of argv, preserving argv order.

    Runs before argparse because argparse dispatches subcommands into a fresh
    namespace and copies it back over the top-level namespace, which drops any
    overrides passed before the subcommand. Scanning argv ourselves keeps the
    full interleaved order regardless of where each flag appears relative to
    the subcommand. ``--config-file`` paths are resolved against the caller's
    cwd so they survive jj's subprocess cwd of ``repo_root``.

    The extractor mirrors argparse/jj semantics for malformed uses: a bare
    ``--config`` (no value), or one whose next token is another option, is left
    in argv so argparse raises its usual "expected one argument" error; and
    everything after the ``--`` end-of-options marker is treated as positional
    and passed through untouched.
    """

    parts: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            remaining.extend(argv[index:])
            break
        flag: str | None = None
        value: str | None = None
        if arg in _CONFIG_OVERRIDE_FLAGS:
            next_arg = argv[index + 1] if index + 1 < len(argv) else None
            if next_arg is None or next_arg.startswith("-"):
                remaining.append(arg)
                index += 1
            else:
                flag = arg
                value = next_arg
                index += 2
        elif "=" in arg:
            head, _, tail = arg.partition("=")
            if head in _CONFIG_OVERRIDE_FLAGS:
                flag = head
                value = tail
                index += 1
            else:
                remaining.append(arg)
                index += 1
                continue
        else:
            remaining.append(arg)
            index += 1
            continue

        if flag is None or value is None:
            continue
        if flag == "--config-file":
            value = str(Path(value).resolve())
        parts.extend((flag, value))

    return JjCliArgs(argv=tuple(parts)), remaining


def _global_cli_args(args: Namespace) -> JjCliArgs:
    return getattr(args, "cli_args", None) or JjCliArgs()


def _completion_handler(args: Namespace) -> int:
    console.output(emit_shell_completion(build_parser(), args.shell), end="")
    return 0


@contextmanager
def _time_output(*, enabled: bool):
    if not enabled:
        yield
        return

    original_print = builtins.print
    at_line_start: dict[int, bool] = {}

    def timed_print(*args, **kwargs) -> None:
        elapsed = time.perf_counter() - APP_START
        destination = kwargs.pop("file", sys.stdout)
        flush = kwargs.pop("flush", False)
        end = kwargs.get("end", "\n")
        buffer = io.StringIO()
        original_print(*args, file=buffer, flush=False, **kwargs)
        rendered = buffer.getvalue()
        if rendered:
            prefix = f"[{elapsed:0.6f}] "
            key = id(destination)
            rendered_output, next_at_line_start = _prefix_rendered_output(
                rendered,
                prefix=prefix,
                at_line_start=at_line_start.get(key, True),
            )
            destination.write(rendered_output)
            at_line_start[key] = next_at_line_start
        elif end:
            key = id(destination)
            rendered_output, next_at_line_start = _prefix_rendered_output(
                end,
                prefix=f"[{elapsed:0.6f}] ",
                at_line_start=at_line_start.get(key, True),
            )
            destination.write(rendered_output)
            at_line_start[key] = next_at_line_start
        if flush:
            destination.flush()

    builtins.print = timed_print  # noqa: B010
    bootstrap.time_output_active = True
    try:
        yield
    finally:
        bootstrap.time_output_active = False
        builtins.print = original_print  # noqa: B010


def _prefix_rendered_output(
    rendered: str,
    *,
    prefix: str,
    at_line_start: bool,
) -> tuple[str, bool]:
    if not rendered:
        return "", at_line_start

    chunks: list[str] = []
    current_at_line_start = at_line_start
    for chunk in rendered.splitlines(keepends=True):
        if current_at_line_start:
            chunks.append(prefix)
        chunks.append(chunk)
        current_at_line_start = chunk.endswith("\n")
    return "".join(chunks), current_at_line_start


def _normalize_cli_args(argv: Sequence[str]) -> list[str]:
    normalized = list(argv)
    for index, arg in enumerate(normalized):
        if not arg.startswith("--draft="):
            continue
        draft_mode = arg.removeprefix("--draft=")
        if draft_mode == "new":
            normalized[index] = "--draft"
            continue
        if draft_mode == "all":
            normalized[index] = "--draft-all"
            continue
        raise CliError(
            t"Invalid value for {ui.cmd('--draft')}: {draft_mode}. Expected new or all."
        )
    return _rewrite_help_args(normalized)


def _extract_reorderable_global_options(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    globals_: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in _REORDERABLE_GLOBAL_FLAGS or any(
            arg.startswith(f"{opt}=") for opt in _REORDERABLE_GLOBAL_OPTIONS_WITH_VALUES
        ):
            globals_.append(arg)
            index += 1
        elif arg in _REORDERABLE_GLOBAL_OPTIONS_WITH_VALUES and index + 1 < len(argv):
            globals_.extend((arg, argv[index + 1]))
            index += 2
        else:
            rest.append(arg)
            index += 1
    return globals_, rest


def _rewrite_help_args(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    starts_with_help = argv[0] == "help"
    scan_limit = argv.index("--") if "--" in argv else len(argv)
    if not starts_with_help and not any(arg in _HELP_FLAGS for arg in argv[:scan_limit]):
        return argv

    source = argv[1:] if starts_with_help else argv
    globals_, rest = _extract_reorderable_global_options(source)

    if starts_with_help:
        return [*globals_, "help", *(arg for arg in rest if arg not in _HELP_FLAGS)]

    subcommands = _KNOWN_COMMANDS - {"help"}
    for arg in rest:
        if arg in _HELP_FLAGS:
            break
        if arg.startswith("-"):
            continue
        if arg in subcommands:
            return [*globals_, "help", arg]
        return argv

    tail = ["--all"] if "--all" in argv else []
    return [*globals_, "help", *tail]


if __name__ == "__main__":
    raise SystemExit(main())
