"""CLI entrypoint for the standalone `jj-stack` executable.

In terminals with hyperlink support, PR labels such as `PR #123` and `#123` are clickable
links to GitHub when the PR's URL is known. Look for them in command results, status output,
and diagnostics. Stack summaries in `submit`, `view`, and `list` link through the top PR;
`list` makes counts such as `5 PRs` clickable too. `--color=never` disables these links.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from argparse import (
    SUPPRESS,
    Action,
    ArgumentParser,
    ArgumentTypeError,
    HelpFormatter,
    Namespace,
    _SubParsersAction,
)
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from inspect import signature
from pathlib import Path
from typing import Any, NoReturn, SupportsIndex

import jj_stack.bootstrap as bootstrap
import jj_stack.commands.checkout as checkout_command
import jj_stack.commands.cleanup.command as cleanup_command
import jj_stack.commands.doctor as doctor_command
import jj_stack.commands.in_use as in_use_command
import jj_stack.commands.list_ as list_command
import jj_stack.commands.merge.command as merge_command
import jj_stack.commands.relink as relink_command
import jj_stack.commands.submit.command as submit_command
import jj_stack.commands.sync as sync_command
import jj_stack.commands.unstack as unstack_command
import jj_stack.commands.view as view_command
import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack import __version__
from jj_stack.cli_help import (
    HelpCommand,
    add_help_argument,
    add_help_section,
    emit_command_help,
    emit_top_level_help,
    normalized_help_text,
    render_website_reference,
)
from jj_stack.completion import emit_shell_completion, validate_jj_alias
from jj_stack.console import RequestedColorMode, configured_console, rich_color_mode
from jj_stack.errors import (
    EXIT_INTERRUPTED,
    CliError,
    UsageError,
    error_hint,
    error_message,
    resolve_exit_code,
)
from jj_stack.jj.cli_args import JjCliArgs

logger = logging.getLogger(__name__)
_COLOR_CHOICES: tuple[RequestedColorMode, ...] = ("always", "never", "debug", "auto")
_TOP_LEVEL_HELP_USAGE = "jj-stack [--help] [--color WHEN] [--version] [<command> ...]"
_TOP_LEVEL_HELP_DESCRIPTION = """
Create and update stacked GitHub pull requests from your `jj` changes.

Edit and rearrange changes with `jj`, then run `jj-stack submit` to update their PRs.
Running `jj-stack` with no command shows the current stack and its PR status.

Use `jj-stack merge` when the PRs at the bottom are ready. The command also updates your local
stack when GitHub merges immediately. After a queued merge finishes, or if you merge on GitHub,
run `jj-stack sync`.
"""
_REORDERABLE_GLOBAL_FLAGS = frozenset({"--debug", "--time-output"})
_REORDERABLE_GLOBAL_OPTIONS_WITH_VALUES = frozenset({"--repository", "--color"})
_HELP_FLAGS = frozenset({"-h", "--help"})
_COMPLETION_HELP = "Print shell completion setup for bash, zsh, or fish"
_HELP_HELP = "Show top-level help, or help for one command"
_COMPLETION_DESCRIPTION = """
Print a shell completion script. Load it from your shell's startup file after any existing
completion setup. For zsh, run it after `compinit`.

If you use a `jj stack` alias, include `--jj-alias stack` to complete commands and options after
both `jj-stack` and `jj stack`. For example:

- Bash: `eval "$(jj-stack completion bash --jj-alias stack)"`

- Zsh: `eval "$(jj-stack completion zsh --jj-alias stack)"`

- Fish: `jj-stack completion fish --jj-alias stack | source`

Omit `--jj-alias` if you only use the standalone `jj-stack` command.
"""
_HELP_DESCRIPTION = """
Show top-level help or the detailed help for one command. Use `--all` to show every command and
global option in top-level help.
"""


_TOP_LEVEL_HELP_GROUPS: tuple[tuple[str, tuple[HelpCommand, ...]], ...] = (
    (
        "Core commands",
        (
            HelpCommand("submit", submit_command.HELP),
            HelpCommand("view", view_command.HELP),
            HelpCommand("list", list_command.HELP),
            HelpCommand("merge", merge_command.HELP),
            HelpCommand("unstack", unstack_command.HELP),
        ),
    ),
    (
        "Support commands",
        (
            HelpCommand("cleanup", cleanup_command.HELP),
            HelpCommand("sync", sync_command.HELP),
            HelpCommand("checkout", checkout_command.HELP),
            HelpCommand("doctor", doctor_command.HELP),
            HelpCommand("in-use", in_use_command.HELP),
        ),
    ),
    (
        "Advanced repair",
        (HelpCommand("relink", relink_command.HELP, hidden=True),),
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
_PR_OPTION_STRINGS = ("-p", "--pull-request")
_COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "submit": ("sub",),
    "view": ("status", "st", "v"),
    "list": ("ls",),
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

    def add_usage(self, usage, actions, groups, prefix=None):  # noqa: ARG002
        return super().add_usage(usage, actions, groups, prefix="Usage: ")


class _OrderedArgument(str):
    """Keep argv position through argparse's slicing and partitioning of attached values."""

    position: int

    def __new__(cls, value: str, position: int):
        result = super().__new__(cls, value)
        result.position = position
        return result

    def __getitem__(self, key: SupportsIndex | slice) -> _OrderedArgument:
        return _OrderedArgument(super().__getitem__(key), self.position)

    def partition(self, sep: str) -> tuple[_OrderedArgument, _OrderedArgument, _OrderedArgument]:
        before, separator, after = super().partition(sep)
        return (
            _OrderedArgument(before, self.position),
            _OrderedArgument(separator, self.position),
            _OrderedArgument(after, self.position),
        )


class _ViewSelectorAction(Action):
    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ARG002
        kind = "pr" if option_string else "revset"
        for value in [values] if option_string else values:
            namespace.selectors += (
                (value.position, view_command.ViewSelector(kind=kind, value=str(value))),
            )


class _CommandArgumentParser(ArgumentParser):
    """ArgumentParser with title-cased built-in help headings."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _TitleCaseHelpFormatter)
        super().__init__(*args, **kwargs)
        self._positionals.title = "Positional Arguments"
        self._optionals.title = "Options"

    def error(self, message: str) -> NoReturn:
        raise _cli_parse_error(message, prog=self.prog)

    def _parse_known_args(self, arg_strings, namespace, intermixed):
        if not any(isinstance(action, _ViewSelectorAction) for action in self._actions):
            return super()._parse_known_args(arg_strings, namespace, intermixed)
        # Python 3.14's shared engine collects intermixed positionals after options. Tag
        # this leaf's input before argparse splits attached values, then restore argv order.
        ordered_args: list[str] = [
            _OrderedArgument(value, position) for position, value in enumerate(arg_strings)
        ]
        parsed, extras = super()._parse_known_args(ordered_args, namespace, intermixed=True)
        parsed.selectors = tuple(selector for _, selector in sorted(parsed.selectors))
        return parsed, extras


def build_parser() -> ArgumentParser:
    """Build the top-level CLI parser and subcommands."""

    # Help rendering owns styling; argparse and its subparsers supply plain usage text.
    parser = _TopLevelArgumentParser(
        prog="jj-stack",
        description=normalized_help_text(_TOP_LEVEL_HELP_DESCRIPTION),
        color=False,
    )
    _add_common_options(parser, suppress_defaults=False)
    parser.set_defaults(command="view", handler=_default_view_handler)
    _normalize_help_action_text(parser)
    add_help_argument(
        parser,
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help=t"Show the {ui.code('jj-stack')} version and exit",
    )

    subcommands = parser.add_subparsers(
        dest="command",
        parser_class=_CommandArgumentParser,
    )
    submit_parser = _add_revset_command(
        subcommands,
        command="submit",
        aliases=_COMMAND_ALIASES["submit"],
        help_text=normalized_help_text(submit_command.HELP),
        description_text=submit_command.__doc__ or "",
        handler=_forward_handler(submit_command.submit, open_="open"),
        revset_help=(
            t"Stack head to submit; defaults to {ui.revset('@')} when the "
            t"working-copy change is described and nonempty, otherwise {ui.revset('@-')}"
        ),
    )
    add_help_section(
        submit_parser,
        title="Supplying descriptions",
        body=submit_command.DESCRIPTION_HELP,
    )
    add_help_argument(
        submit_parser,
        "--base",
        metavar="REVSET",
        help=(
            "Submit changes above this submitted ancestor, using its PR branch as the base; "
            "repeat this option on later submits of the dependent stack"
        ),
    )
    add_help_argument(
        submit_parser,
        "--dry-run",
        action="store_true",
        help="Preview submission without pushing branches or changing pull requests",
    )
    submit_description_mode = submit_parser.add_mutually_exclusive_group()
    add_help_argument(
        submit_description_mode,
        "--describe",
        dest="descriptions",
        metavar="TARGET=FILE",
        action="append",
        help=(
            t"Read a PR body from {ui.metavar('FILE')}; {ui.metavar('TARGET')} is a change ID "
            t"or {ui.code('stack')} for an overview comment on the head PR"
        ),
    )
    add_help_argument(
        submit_description_mode,
        "--describe-with",
        metavar="HELPER",
        help=(
            t"Generate pull request titles, bodies, and the stack overview with "
            t"{ui.metavar('HELPER')}"
        ),
    )
    submit_edit_mode = submit_parser.add_mutually_exclusive_group()
    add_help_argument(
        submit_edit_mode,
        "--edit",
        action="store_true",
        default=False,
        help=(
            "Open planned pull request titles, bodies, and draft states in your editor before "
            "submitting"
        ),
    )
    add_help_argument(
        submit_edit_mode,
        "--resume-edit",
        dest="edit",
        metavar="FILE",
        type=Path,
        help="Reopen a saved editor file instead of generating a new one",
    )
    submit_draft_mode = submit_parser.add_mutually_exclusive_group()
    add_help_argument(
        submit_draft_mode,
        "--draft",
        action="store_true",
        help=(
            t"Create new PRs as drafts; use {ui.option('--draft=all')} to make existing "
            t"PRs drafts too"
        ),
    )
    submit_draft_mode.add_argument(
        "--draft-all",
        action="store_true",
        help=SUPPRESS,
    )
    submit_draft_mode.add_argument(
        "--open",
        dest="open",
        action="store_true",
        help="Mark submitted PRs ready for review, including existing drafts",
    )
    add_help_argument(
        submit_parser,
        "--label",
        dest="labels",
        action="append",
        help="Add labels to the selected PRs; comma-separated or repeat the option",
    )
    add_help_argument(
        submit_parser,
        "--reviewers",
        dest="reviewers",
        action="append",
        metavar="USERS",
        help="Request reviews by GitHub username; comma-separated or repeat the option",
    )
    add_help_argument(
        submit_parser,
        "--team-reviewers",
        dest="team_reviewers",
        action="append",
        metavar="TEAMS",
        help="Request reviews by team slug; comma-separated or repeat the option",
    )
    add_help_argument(
        submit_parser,
        "--re-request",
        action="store_true",
        help=(
            "Request another review from users who last approved or requested changes on an "
            "existing pull request"
        ),
    )
    view_parser = _add_command_parser(
        subcommands,
        command="view",
        aliases=_COMMAND_ALIASES["view"],
        help_text=normalized_help_text(view_command.HELP),
        description_text=view_command.__doc__ or "",
        handler=_forward_handler(view_command.view, *_VIEW_HANDLER_ARGS),
    )
    view_parser.set_defaults(selectors=())
    add_help_argument(
        view_parser,
        "selectors",
        metavar="revset",
        nargs="*",
        action=_ViewSelectorAction,
        help=(
            t"Select a stack by its head revset or by any change ID it contains. Combine either "
            t"with {ui.option('--pull-request')}; defaults to the current stack"
        ),
    )
    add_help_argument(
        view_parser,
        *_PR_OPTION_STRINGS,
        dest="selectors",
        metavar="PR",
        action=_ViewSelectorAction,
        help=(
            "Inspect the full stack containing this PR number or URL; repeat to inspect "
            "several stacks"
        ),
    )
    view_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Output stack status as JSON",
    )
    view_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show every change instead of collapsing the middle of a long stack",
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
        "--json",
        dest="as_json",
        action="store_true",
        help="Output tracked stacks and orphaned PRs as JSON",
    )
    _add_relink_parser(
        subcommands,
        command="relink",
        help_text=normalized_help_text(relink_command.HELP),
        description_text=relink_command.__doc__ or "",
        handler=_forward_handler(relink_command.relink),
    )
    merge_parser = _add_revset_command(
        subcommands,
        command="merge",
        help_text=normalized_help_text(merge_command.HELP),
        description_text=merge_command.__doc__ or "",
        handler=_forward_handler(merge_command.merge),
        revset_help=(
            t"Stack head to merge; defaults to {ui.revset('@')} when the working-copy change "
            t"is described and nonempty, otherwise {ui.revset('@-')}. To merge only the bottom "
            t"portion, use {ui.option('--pull-request')} instead"
        ),
    )
    merge_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the merge without asking GitHub to merge anything",
    )
    add_help_argument(
        merge_parser,
        *_PR_OPTION_STRINGS,
        dest="pr",
        metavar="PR",
        help=(
            "Merge this PR and all PRs below it; when GitHub merges immediately, also sync the "
            "rest of the stack"
        ),
    )
    add_help_argument(
        merge_parser,
        "--method",
        dest="merge_method",
        choices=("merge", "rebase", "squash"),
        metavar="METHOD",
        help=(
            t"GitHub merge method: {ui.metavar('merge')}, {ui.metavar('rebase')}, or "
            t"{ui.metavar('squash')}. Defaults to {ui.code('jj-stack.merge_method')}, or the "
            t"repo's only allowed method. Otherwise prefers rebase, squash, then merge. "
            t"For signed commits, choose a method if several are allowed: merging can discard "
            t"signatures. "
            t"Merge queues choose their own method"
        ),
    )
    unstack_parser = _add_revset_command(
        subcommands,
        command="unstack",
        help_text=normalized_help_text(unstack_command.HELP),
        description_text=unstack_command.__doc__ or "",
        handler=_forward_handler(unstack_command.unstack),
        revset_help=(
            t"Stack head to unstack; defaults to {ui.revset('@')} when the "
            t"working-copy change is described and nonempty, otherwise {ui.revset('@-')}; "
            t"cannot be combined with {ui.option('--pull-request')} or {ui.option('--stack')}"
        ),
    )
    unstack_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview separating the GitHub stack or forgetting saved links locally",
    )
    unstack_parser.add_argument(
        "--local",
        action="store_true",
        help="Only forget saved pull request links; do not change GitHub",
    )
    add_help_argument(
        unstack_parser,
        *_PR_OPTION_STRINGS,
        dest="pr",
        metavar="PR",
        help="Select the local stack linked to this pull request number or URL",
    )
    add_help_argument(
        unstack_parser,
        "--stack",
        type=int,
        metavar="NUMBER",
        help="Separate this GitHub stack even when no matching local stack is available",
    )
    _add_checkout_parser(
        subcommands,
        command="checkout",
        help_text=normalized_help_text(checkout_command.HELP),
        description_text=checkout_command.__doc__ or "",
        handler=_forward_handler(checkout_command.checkout),
    )

    cleanup_parser = _add_revset_command(
        subcommands,
        command="cleanup",
        help_text=normalized_help_text(cleanup_command.HELP),
        description_text=cleanup_command.__doc__ or "",
        handler=_forward_handler(cleanup_command.cleanup),
        revset_help=(
            t"Revset selecting the stack to clean up; omit it to check every "
            t"tracked pull request; cannot be combined with {ui.option('--pull-request')}"
        ),
    )
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview cleanup without closing PRs or removing anything",
    )
    add_help_argument(
        cleanup_parser,
        "--close",
        action="store_true",
        help=(
            t"Close selected open pull requests before cleanup; requires "
            t"{ui.option('--pull-request')}"
        ),
    )
    add_help_argument(
        cleanup_parser,
        *_PR_OPTION_STRINGS,
        dest="pr",
        metavar="PR",
        help=(
            t"Clean up this tracked pull request, or use {ui.metavar('orphans')} for every "
            t"tracked pull request whose local change is gone"
        ),
    )

    sync_parser = _add_revset_command(
        subcommands,
        command="sync",
        help_text=normalized_help_text(sync_command.HELP),
        description_text=sync_command.__doc__ or "",
        handler=_forward_handler(sync_command.sync, all_="all"),
        revset_help=(
            t"Stack head to sync; defaults to {ui.revset('@')} when the "
            t"working-copy change is described and nonempty, otherwise {ui.revset('@-')}; "
            t"cannot be combined with {ui.option('--pull-request')}"
        ),
    )
    add_help_argument(
        sync_parser,
        "--dry-run",
        action="store_true",
        help=(
            "Preview the sync without changing your local stack, PRs, branches, or saved links"
        ),
    )
    add_help_argument(
        sync_parser,
        *_PR_OPTION_STRINGS,
        dest="pr",
        metavar="PR",
        help="Sync the complete local stack containing this pull request number or URL",
    )
    add_help_argument(
        sync_parser,
        "-a",
        "--all",
        action="store_true",
        help=(
            "Sync every stack affected by a completed merge, including merged PRs whose local "
            "changes are gone; cannot be combined with a selector"
        ),
    )

    doctor_parser = _add_command_parser(
        subcommands,
        command="doctor",
        help_text=normalized_help_text(doctor_command.HELP),
        description_text=doctor_command.__doc__ or "",
        handler=_forward_handler(doctor_command.doctor),
    )
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply safe local repairs instead of only reporting problems",
    )

    _add_command_parser(
        subcommands,
        command="in-use",
        help_text=normalized_help_text(in_use_command.HELP),
        description_text=in_use_command.__doc__ or "",
        handler=_forward_handler(in_use_command.in_use),
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
    add_help_argument(
        completion_parser,
        "--jj-alias",
        metavar="NAME",
        type=_parse_jj_alias,
        help=(
            t"Also complete an existing {ui.code('jj')} alias that runs {ui.code('jj-stack')}, "
            t"such as {ui.code('stack')}"
        ),
    )
    help_parser = _add_command_parser(
        subcommands,
        command="help",
        help_text=SUPPRESS,
        description_text=_HELP_DESCRIPTION,
        handler=_help_handler,
        common_options=False,
    )
    help_scope = help_parser.add_mutually_exclusive_group()
    add_help_argument(
        help_scope,
        "--all",
        action="store_true",
        help="Show every command and global option in top-level help",
    )
    help_scope.add_argument("--website-reference", action="store_true", help=SUPPRESS)
    help_parser.add_argument(
        "command",
        nargs="?",
        help="Command to describe",
    )
    return parser


def _help_handler(args: Namespace) -> int:
    parser = build_parser()
    if args.website_reference:
        if args.command is not None:
            raise UsageError("help --website-reference cannot be combined with a command")
        console.output(
            render_website_reference(
                parser,
                groups=_TOP_LEVEL_HELP_GROUPS,
                aliases=_COMMAND_ALIASES,
            ),
            end="",
            soft_wrap=True,
        )
        return 0
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
        repo=None,
        requested_color_mode=requested_color_mode,
        time_output=False,
    ):
        _print_cli_error(error)


def _cli_parse_error(message: str, *, prog: str | None = None) -> CliError:
    message = message.strip()
    invalid_choice = re.match(
        r"argument (?P<argument>[^:]+): invalid choice: '(?P<value>[^']+)'(?: .*)?$",
        message,
    )
    if invalid_choice is not None and invalid_choice.group("argument") == "command":
        return _unknown_command_error(invalid_choice.group("value"))
    unrecognized = message.lower().startswith("unrecognized argument")
    if message and not message.endswith("."):
        message = f"{message}."
    if message:
        message = f"{message[0].upper()}{message[1:]}"
    if unrecognized:
        command = prog.split()[-1] if prog and " " in prog else None
        listing = f"jj-stack help {command}" if command else "jj-stack help <command>"
        return UsageError(
            message,
            hint=t"Run {ui.cmd(listing)} to list the options a command accepts.",
        )
    return UsageError(message)


def _unknown_command_error(command_name: str) -> CliError:
    return UsageError(
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
            return value
        return None
    return None


def _load_configured_jj_color(
    *,
    repo: Path | None,
    cli_args: JjCliArgs,
) -> RequestedColorMode | None:
    """Read `ui.color` from `jj` config without requiring repo bootstrap."""

    cwd = repo if repo is not None and repo.exists() and repo.is_dir() else Path.cwd()
    try:
        completed = subprocess.run(
            ["jj", *cli_args.argv, "--ignore-working-copy", "config", "get", "ui.color"],
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
        )
    except FileNotFoundError, OSError:
        return None

    if completed.returncode != 0:
        return None

    configured = completed.stdout.strip()
    if configured in _COLOR_CHOICES:
        return configured
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    cli_args = JjCliArgs()
    normalized_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cli_args, stripped_argv = _extract_config_overrides(normalized_argv)
        normalized_argv = _normalize_cli_args(stripped_argv)
        args = parser.parse_args(normalized_argv)
    except CliError as error:
        _print_early_cli_error(
            error,
            cli_args=cli_args,
            normalized_argv=normalized_argv,
        )
        return resolve_exit_code(error)
    args.cli_args = cli_args
    args.normalized_argv = tuple(normalized_argv)
    effective_color = "never" if args.command == "in-use" else args.color
    try:
        if effective_color is None:
            effective_color = _load_configured_jj_color(
                repo=args.repo,
                cli_args=cli_args,
            )
        with configured_console(
            cli_args=cli_args,
            color_mode=rich_color_mode(effective_color),
            repo=args.repo,
            requested_color_mode=args.color,
            time_output=args.time_output,
        ):
            with _time_output(enabled=args.time_output):
                handler = args.handler
                try:
                    return handler(args)
                except CliError as error:
                    _print_cli_error(error)
                    return resolve_exit_code(error)
                except KeyboardInterrupt:
                    return _report_interrupt()
    except KeyboardInterrupt:
        return _report_interrupt()


def _report_interrupt() -> int:
    """Report an interrupt on whichever stderr console is installed."""

    console.stderr_output("Interrupted.")
    return EXIT_INTERRUPTED


def _default_view_handler(args: Namespace) -> int:
    """Run bare `jj-stack` as the default `view` command."""

    return view_command.view(
        cli_args=args.cli_args,
        debug=args.debug,
        as_json=False,
        repo=args.repo,
        selectors=(),
        verbose=False,
    )


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
    description = normalized_help_text(description_text)
    if aliases:
        spelled = ", ".join(f"jj-stack {alias}" for alias in aliases)
        description = f"{description}\n\nAlso spelled {spelled}."
    parser = subcommands.add_parser(
        command,
        aliases=list(aliases),
        help=help_text,
        description=description,
    )
    if common_options:
        _add_common_options(parser)
    _normalize_help_action_text(parser)
    parser.set_defaults(handler=handler)
    return parser


def _add_revset_command(
    subcommands: _SubParsersAction[Any],
    *,
    command: str,
    aliases: Sequence[str] = (),
    help_text: str,
    description_text: str,
    handler: Callable[[Namespace], int],
    revset_help: ui.Message | str = "Revset selecting the stack to operate on",
) -> ArgumentParser:
    parser = _add_command_parser(
        subcommands,
        command=command,
        aliases=aliases,
        help_text=help_text,
        description_text=description_text,
        handler=handler,
    )
    add_help_argument(parser, "revset", nargs="?", help=revset_help)
    return parser


def _add_relink_parser(
    subcommands: _SubParsersAction[Any],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler: Callable[[Namespace], int],
) -> None:
    parser = _add_command_parser(
        subcommands,
        command=command,
        help_text=help_text,
        description_text=description_text,
        handler=handler,
    )
    add_help_argument(parser, "pr", metavar="PR", help="Pull request number or URL")
    add_help_argument(
        parser,
        "revset",
        metavar="REVSET",
        help="Local change to link to the pull request",
    )
    add_help_argument(
        parser,
        "--replace-remote",
        action="store_true",
        help=(
            t"Link even if the PR branch has changed unexpectedly; the next "
            t"{ui.cmd('jj-stack submit')} overwrites it with the local change"
        ),
    )


def _add_checkout_parser(
    subcommands: _SubParsersAction[Any],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler: Callable[[Namespace], int],
) -> None:
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
        *_PR_OPTION_STRINGS,
        dest="pr",
        metavar="PR",
        help="Pull request to check out, by number or URL",
    )
    add_help_argument(
        selector,
        "--revset",
        help=(
            t"Edit the head of a locally tracked stack without contacting GitHub; "
            t"defaults to {ui.revset('@')} when the working-copy change is described and "
            t"nonempty, otherwise {ui.revset('@-')}"
        ),
    )
    add_help_argument(
        selector,
        "--pick",
        action="store_true",
        help="Interactively choose a local or GitHub stack to check out",
    )


def _add_common_options(
    parser: ArgumentParser,
    *,
    suppress_defaults: bool = True,
) -> None:
    parser.add_argument(
        "--repository",
        dest="repo",
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
    add_help_argument(
        parser,
        "--config",
        action="store",
        default=SUPPRESS,
        dest=SUPPRESS,
        metavar="NAME=VALUE",
        help=(
            t"Set a {ui.code('jj')} config value for this command, such as "
            t"{ui.code('ui.color=always')}; repeat for several values"
        ),
    )
    add_help_argument(
        parser,
        "--config-file",
        action="store",
        default=SUPPRESS,
        dest=SUPPRESS,
        metavar="PATH",
        help=t"Additional {ui.code('jj')} config file to load; repeat for several",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=SUPPRESS if suppress_defaults else False,
        help="Enable debug logging",
    )
    add_help_argument(
        parser,
        "--color",
        choices=_COLOR_CHOICES,
        default=SUPPRESS if suppress_defaults else None,
        metavar="WHEN",
        help=(t"When to colorize output; possible values: {ui.join(ui.metavar, _COLOR_CHOICES)}"),
    )
    parser.add_argument(
        "--time-output",
        action="store_true",
        default=SUPPRESS if suppress_defaults else False,
        help="Prefix each output line with elapsed seconds",
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
            name,
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
    # soft_wrap keeps the shell from receiving a script wrapped to the console width, which
    # splits long case patterns mid-word and makes it unparseable.
    console.output(
        emit_shell_completion(build_parser(), args.shell, jj_alias=args.jj_alias),
        end="",
        soft_wrap=True,
    )
    return 0


def _parse_jj_alias(value: str) -> str:
    try:
        return validate_jj_alias(value)
    except ValueError as error:
        raise ArgumentTypeError(str(error)) from error


@contextmanager
def _time_output(*, enabled: bool):
    if not enabled:
        yield
        return

    bootstrap.time_output_active = True
    try:
        yield
    finally:
        bootstrap.time_output_active = False


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
        raise UsageError(
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
