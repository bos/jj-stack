"""CLI entrypoint for the standalone `jj-stack` executable."""

from __future__ import annotations

import builtins
import io
import logging
import re
import subprocess
import sys
import time
from argparse import (
    SUPPRESS,
    ArgumentParser,
    HelpFormatter,
    Namespace,
    _SubParsersAction,
)
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from typing import Any, NoReturn, cast

import jj_stack.bootstrap as bootstrap
import jj_stack.commands.checkout as checkout_command
import jj_stack.commands.cleanup.command as cleanup_command
import jj_stack.commands.doctor as doctor_command
import jj_stack.commands.land.command as land_command
import jj_stack.commands.list_ as list_command
import jj_stack.commands.relink as relink_command
import jj_stack.commands.restart as restart_command
import jj_stack.commands.submit.command as submit_command
import jj_stack.commands.unlink as unlink_command
import jj_stack.commands.unstack as unstack_command
import jj_stack.commands.view as view_command
import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack import __version__
from jj_stack.bootstrap import APP_START
from jj_stack.cli_help import (
    HelpCommand,
    add_help_argument,
    emit_command_help,
    emit_top_level_help,
    normalized_help_text,
)
from jj_stack.completion import emit_shell_completion
from jj_stack.console import RequestedColorMode, configured_console, rich_color_mode
from jj_stack.errors import CliError, error_hint, error_message
from jj_stack.jj.client import JjCliArgs

logger = logging.getLogger(__name__)
_COLOR_CHOICES: tuple[RequestedColorMode, ...] = ("always", "never", "debug", "auto")
_TOP_LEVEL_HELP_USAGE = "jj-stack [--help] [--color WHEN] [--version] [<command> ...]"
_TOP_LEVEL_HELP_DESCRIPTION = """
`jj-stack` lets you review a series of `jj` changes on GitHub as stacked pull requests.

Use it to submit and refresh changes for review, inspect pull request status, land ready
changes, list locally known stacks, and clean up after a review.
"""
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


_TOP_LEVEL_HELP_GROUPS: tuple[tuple[str, tuple[HelpCommand, ...]], ...] = (
    (
        "Core commands",
        (
            HelpCommand("submit", submit_command.HELP),
            HelpCommand("view", view_command.HELP),
            HelpCommand("list", list_command.HELP),
            HelpCommand("land", land_command.HELP),
            HelpCommand("unstack", unstack_command.HELP),
        ),
    ),
    (
        "Support commands",
        (
            HelpCommand("cleanup", cleanup_command.HELP),
            HelpCommand("checkout", checkout_command.HELP),
            HelpCommand("doctor", doctor_command.HELP),
        ),
    ),
    (
        "Advanced repair",
        (
            HelpCommand("restart", restart_command.HELP, hidden=True),
            HelpCommand("relink", relink_command.HELP, hidden=True),
            HelpCommand("unlink", unlink_command.HELP, hidden=True),
        ),
    ),
    (
        "Configuration",
        (HelpCommand("completion", _COMPLETION_HELP, hidden=True),),
    ),
    (
        "Help",
        (HelpCommand("help", _HELP_HELP, hidden=True),),
    ),
)
_PULL_REQUEST_OPTION_STRINGS = ("-p", "--pull-request")
_COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "submit": ("sub",),
    "list": ("ls",),
    "unstack": ("delete",),
}
_KNOWN_COMMANDS = frozenset(
    name
    for _, entries in _TOP_LEVEL_HELP_GROUPS
    for entry in entries
    for name in (entry.name, *_COMMAND_ALIASES.get(entry.name, ()))
)
type _ArgSource = str | Callable[[Namespace], Any]


_VIEW_HANDLER_ARGS = tuple(
    name
    for name, parameter in signature(view_command.view).parameters.items()
    if parameter.kind is not parameter.VAR_KEYWORD
)


class _TopLevelArgumentParser(ArgumentParser):
    """ArgumentParser with custom grouped help for the top-level CLI."""

    def format_usage(self) -> str:
        return f"usage: {_TOP_LEVEL_HELP_USAGE}\n"

    def error(self, message: str) -> NoReturn:
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

    def error(self, message: str) -> NoReturn:
        raise _cli_parse_error(message)


def build_parser() -> ArgumentParser:
    """Build the top-level CLI parser and subcommands."""

    parser = _TopLevelArgumentParser(
        prog="jj-stack",
        description=normalized_help_text(_TOP_LEVEL_HELP_DESCRIPTION),
    )
    _add_common_options(parser, suppress_defaults=False)
    parser.set_defaults(command="view", handler=_default_view_handler)
    _normalize_help_action_text(parser)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit",
    )

    subcommands = parser.add_subparsers(
        dest="command",
        parser_class=_CommandArgumentParser,
    )
    submit_parser = _add_revision_command(
        subcommands,
        command="submit",
        aliases=_COMMAND_ALIASES["submit"],
        help_text=normalized_help_text(submit_command.HELP),
        description_text=submit_command.__doc__ or "",
        handler=_forward_handler(submit_command.submit),
        revset_help=(
            t"Revision to submit; defaults to {ui.revset('@-')} (the current stack head)"
        ),
    )
    add_help_argument(
        submit_parser,
        "--dry-run",
        action="store_true",
        help="Print the submit plan without making any changes",
    )
    add_help_argument(
        submit_parser,
        "-d",
        "--describe-with",
        metavar="HELPER",
        help="Delegate pull request and stack-comment text generation to HELPER",
    )
    submit_draft_mode = submit_parser.add_mutually_exclusive_group()
    add_help_argument(
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
    add_help_argument(
        submit_parser,
        "--label",
        dest="labels",
        action="append",
        help="Apply GitHub labels to submitted pull requests",
    )
    add_help_argument(
        submit_parser,
        "--reviewers",
        dest="reviewers",
        action="append",
        metavar="USERS",
        help="Request reviews from GitHub users on submitted pull requests",
    )
    add_help_argument(
        submit_parser,
        "--team-reviewers",
        dest="team_reviewers",
        action="append",
        metavar="TEAMS",
        help="Ask for reviews from GitHub teams on submitted pull requests",
    )
    add_help_argument(
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
    add_help_argument(
        submit_parser,
        "--re-request",
        action="store_true",
        help=(
            "Request review again from users whose latest review on an existing "
            "pull request approved it or requested changes"
        ),
    )
    add_help_argument(
        submit_parser,
        "--restart",
        action="store_true",
        help="Forget previous PR tracking for selected changes and create fresh PRs",
    )
    view_parser = _add_revision_command(
        subcommands,
        command="view",
        help_text=normalized_help_text(view_command.HELP),
        description_text=view_command.__doc__ or "",
        handler=_forward_handler(
            view_command.view,
            *_VIEW_HANDLER_ARGS,
            selectors=lambda args: args.view_selectors,
        ),
        revset_help=(
            "Revsets to inspect; can be mixed with repeated --pull-request selectors; "
            "defaults to the current stack"
        ),
        revset_nargs="*",
    )
    add_help_argument(
        view_parser,
        *_PULL_REQUEST_OPTION_STRINGS,
        metavar="PR",
        action="append",
        help="Inspect the stack for this PR number or URL; repeat to inspect several stacks",
    )
    view_parser.add_argument(
        "-f",
        "--fetch",
        action="store_true",
        help="Fetch first so view uses current remote branch locations",
    )
    view_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Expand submitted and unsubmitted summary sections; keep native jj log lines",
    )
    list_parser = _add_command_parser(
        subcommands,
        command="list",
        aliases=list(_COMMAND_ALIASES["list"]),
        help_text=normalized_help_text(list_command.HELP),
        description_text=list_command.__doc__ or "",
        handler=_forward_handler(list_command.list_),
    )
    list_parser.add_argument(
        "-f",
        "--fetch",
        action="store_true",
        help="Fetch first so list uses current remote branch locations",
    )
    _add_relink_parser(
        subcommands,
        command="relink",
        help_text=normalized_help_text(relink_command.HELP),
        description_text=relink_command.__doc__ or "",
        handler=_forward_handler(relink_command.relink),
    )
    restart_parser = _add_revision_command(
        subcommands,
        command="restart",
        help_text=normalized_help_text(restart_command.HELP),
        description_text=restart_command.__doc__ or "",
        handler=_forward_handler(restart_command.restart),
        revset_nargs=None,
        revset_help="Stack head to prepare for fresh pull requests",
    )
    restart_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be reset without changing tracking data",
    )
    _add_revision_command(
        subcommands,
        command="unlink",
        help_text=normalized_help_text(unlink_command.HELP),
        description_text=unlink_command.__doc__ or "",
        handler=_forward_handler(unlink_command.unlink),
        revset_nargs=None,
        revset_help="Revision to unlink",
    )
    land_parser = _add_revision_command(
        subcommands,
        command="land",
        help_text=normalized_help_text(land_command.HELP),
        description_text=land_command.__doc__ or "",
        handler=_forward_handler(land_command.land),
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
    add_help_argument(
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
    unstack_parser = _add_revision_command(
        subcommands,
        command="unstack",
        aliases=_COMMAND_ALIASES["unstack"],
        help_text=normalized_help_text(unstack_command.HELP),
        description_text=unstack_command.__doc__ or "",
        handler=_forward_handler(unstack_command.unstack),
        revset_help=(
            t"Revision to unstack; defaults to {ui.revset('@-')} (the current stack head); "
            t"cannot be combined with {ui.cmd('--pull-request')}"
        ),
    )
    unstack_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the unstack plan without making any changes",
    )
    unstack_parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete jj-stack-managed branches, bookmarks, and tracking data",
    )
    add_help_argument(
        unstack_parser,
        *_PULL_REQUEST_OPTION_STRINGS,
        metavar="PR",
        help=(
            "Select a stack by PR number or URL; with --cleanup, this can also retire "
            "an orphaned PR shown by list"
        ),
    )
    _add_checkout_parser(
        subcommands,
        command="checkout",
        help_text=normalized_help_text(checkout_command.HELP),
        description_text=checkout_command.__doc__ or "",
        handler=_forward_handler(checkout_command.checkout),
    )

    cleanup_parser = _add_command_parser(
        subcommands,
        command="cleanup",
        help_text=normalized_help_text(cleanup_command.HELP),
        description_text=cleanup_command.__doc__ or "",
        handler=_forward_handler(
            cleanup_command.cleanup,
            rebase_revset="rebase",
        ),
    )
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cleanup actions without making any changes",
    )
    add_help_argument(
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

    _add_command_parser(
        subcommands,
        command="doctor",
        help_text=normalized_help_text(doctor_command.HELP),
        description_text=doctor_command.__doc__ or "",
        handler=_forward_handler(doctor_command.doctor),
    )

    completion_parser = _add_command_parser(
        subcommands,
        command="completion",
        help_text=_COMPLETION_HELP,
        description_text=_COMPLETION_DESCRIPTION,
        handler=_completion_handler,
        common_options=False,
    )
    completion_parser.add_argument(
        "shell",
        choices=("bash", "zsh", "fish"),
        help="Shell to generate completion support for",
    )
    help_parser = _add_command_parser(
        subcommands,
        command="help",
        help_text=SUPPRESS,
        description_text=_HELP_DESCRIPTION,
        handler=_help_handler,
        common_options=False,
    )
    add_help_argument(
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
    return parser


def _help_handler(args: Namespace) -> int:
    parser = build_parser()
    if args.command is None:
        emit_top_level_help(
            parser,
            groups=_TOP_LEVEL_HELP_GROUPS,
            aliases=_COMMAND_ALIASES,
            include_hidden=args.all,
        )
        return 0

    command_parser = _find_subcommand_parser(parser, args.command)
    if command_parser is None:
        raise _unknown_command_error(args.command)
    emit_command_help(command_parser)
    return 0


def _find_subcommand_parser(
    parser: ArgumentParser,
    command_name: str,
) -> ArgumentParser | None:
    for action in parser._actions:
        if isinstance(action, _SubParsersAction):
            parser_choice = action.choices.get(command_name)
            return parser_choice if isinstance(parser_choice, ArgumentParser) else None
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
        hint=t"Run {ui.cmd('jj-stack help')} to list commands.",
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    cli_args = JjCliArgs()
    normalized_argv = list(sys.argv[1:] if argv is None else argv)
    view_args: _ParsedViewCommandArgs | None = None
    try:
        cli_args, stripped_argv = _extract_config_overrides(normalized_argv)
        normalized_argv = _normalize_cli_args(stripped_argv)
        view_args = _parse_view_command_args(normalized_argv)
        if view_args is not None:
            normalized_argv = list(view_args.argv)
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
    if args.command == "view":
        args.view_selectors = () if view_args is None else view_args.selectors
    effective_color = args.color
    if effective_color is None:
        effective_color = _load_configured_jj_color(
            repository=args.repository,
            cli_args=cli_args,
        )
    with configured_console(
        cli_args=cli_args,
        color_mode=rich_color_mode(effective_color),
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


def _default_view_handler(args: Namespace) -> int:
    """Run bare `jj-stack` as the default `view` command."""

    return view_command.view(
        cli_args=args.cli_args,
        debug=args.debug,
        fetch=False,
        pull_request=None,
        repository=args.repository,
        revset=None,
        selectors=(),
        verbose=False,
    )


@dataclass(frozen=True)
class _ParsedViewCommandArgs:
    argv: tuple[str, ...]
    selectors: tuple[view_command.ViewSelector, ...]


def _parse_view_command_args(argv: Sequence[str]) -> _ParsedViewCommandArgs | None:
    """Rewrite `view` argv and preserve explicit selector order."""

    command_index = _find_subcommand_index(argv)
    if command_index is None or argv[command_index] != "view":
        return None

    prefix = list(argv[: command_index + 1])
    command_argv = argv[command_index + 1 :]
    options: list[str] = []
    revsets: list[str] = []
    selectors: list[view_command.ViewSelector] = []
    index = 0
    while index < len(command_argv):
        arg = command_argv[index]
        if arg == "--":
            trailing_revsets = command_argv[index + 1 :]
            revsets.extend(trailing_revsets)
            selectors.extend(
                view_command.ViewSelector(kind="revset", value=value)
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
                    view_command.ViewSelector(
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
                    view_command.ViewSelector(
                        kind="pull_request",
                        value=value,
                    )
                )
            index += 1
            continue
        if arg in _VIEW_SELECTOR_FLAGS or _is_grouped_view_flag(arg):
            options.append(arg)
            index += 1
            continue
        if arg.startswith("-"):
            options.append(arg)
            index += 1
            continue
        revsets.append(arg)
        selectors.append(view_command.ViewSelector(kind="revset", value=arg))
        index += 1
    normalized = [*prefix, *options]
    if revsets:
        normalized.extend(["--", *revsets])
    return _ParsedViewCommandArgs(argv=tuple(normalized), selectors=tuple(selectors))


_VIEW_SELECTOR_FLAGS = frozenset(
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


def _is_grouped_view_flag(arg: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and set(arg[1:]) <= {"f", "v", "h"}


def _add_command_parser(
    subcommands: _SubParsersAction[Any],
    *,
    command: str,
    aliases: Sequence[str] = (),
    help_text: str,
    description_text: str,
    handler: Callable[[Namespace], int],
    common_options: bool = True,
) -> ArgumentParser:
    parser = subcommands.add_parser(
        command,
        aliases=list(aliases),
        help=help_text,
        description=normalized_help_text(description_text),
    )
    if common_options:
        _add_common_options(parser)
    _normalize_help_action_text(parser)
    parser.set_defaults(handler=handler)
    return parser


def _add_revision_command(
    subcommands: _SubParsersAction[Any],
    *,
    command: str,
    aliases: Sequence[str] = (),
    help_text: str,
    description_text: str,
    handler: Callable[[Namespace], int],
    revset_nargs: str | int | None = "?",
    revset_help: ui.Message | str = "Revision to operate on",
) -> ArgumentParser:
    parser = _add_command_parser(
        subcommands,
        command=command,
        aliases=aliases,
        help_text=help_text,
        description_text=description_text,
        handler=handler,
    )
    add_help_argument(parser, "revset", nargs=revset_nargs, help=revset_help)
    return parser


def _add_relink_parser(
    subcommands: _SubParsersAction[Any],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler: Callable[[Namespace], int],
) -> ArgumentParser:
    parser = _add_command_parser(
        subcommands,
        command=command,
        help_text=help_text,
        description_text=description_text,
        handler=handler,
    )
    add_help_argument(parser, "pull_request", help="Pull request number or URL")
    add_help_argument(parser, "revset", help="Revision to reassociate with the pull request")
    return parser


def _add_checkout_parser(
    subcommands: _SubParsersAction[Any],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler: Callable[[Namespace], int],
) -> ArgumentParser:
    parser = _add_command_parser(
        subcommands,
        command=command,
        help_text=help_text,
        description_text=description_text,
        handler=handler,
    )
    selector = parser.add_mutually_exclusive_group(required=False)
    add_help_argument(
        selector,
        *_PULL_REQUEST_OPTION_STRINGS,
        metavar="PR",
        help="Pull request number or URL",
    )
    add_help_argument(
        selector,
        "--revset",
        help="Explicit revset whose exact stack should be checked out",
    )
    add_help_argument(
        parser,
        "--fetch",
        action="store_true",
        help=(
            t"Refresh the selected stack's remote bookmark state and, for "
            t"{ui.cmd('--pull-request')}, fetch only the branches needed to import "
            t"that stack"
        ),
    )
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
    return args.cli_args


def _forward_handler(
    function: Callable[..., int],
    *fallback_arg_names: str,
    **arg_sources: _ArgSource,
) -> Callable[[Namespace], int]:
    """Build a command handler that forwards argparse values as keyword arguments."""

    parameters = signature(function).parameters
    if any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()):
        parameter_names = fallback_arg_names
    else:
        parameter_names = tuple(
            name
            for name, parameter in parameters.items()
            if parameter.kind is not parameter.VAR_KEYWORD
        )
    parameter_sources: dict[str, _ArgSource] = dict(arg_sources)
    for name in parameter_names:
        parameter_sources[name] = arg_sources.get(
            name,
            _global_cli_args if name == "cli_args" else name,
        )

    def handler(args: Namespace) -> int:
        values = vars(args)
        return function(
            **{
                name: source(args) if not isinstance(source, str) else values[source]
                for name, source in parameter_sources.items()
            }
        )

    return handler


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
