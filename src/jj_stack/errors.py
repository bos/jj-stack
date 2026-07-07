"""User-facing error types and exit codes shared across CLI commands."""

from __future__ import annotations

from typing import Literal

from jj_stack.ui import Message, plain_text

type ErrorMessage = Message
type ErrorHint = Message

# Process exit codes. Codes 2-6 carry the same meanings as the matching
# `gh stack` exit codes so scripted callers can treat the two tools alike;
# 7-9 are reserved because their `gh stack` meanings have no jj-stack analog.
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_NO_STACK = 2
EXIT_CONFLICTS = 3
EXIT_GITHUB = 4
EXIT_USAGE = 5
EXIT_AMBIGUOUS = 6
EXIT_INCOMPLETE = 10
EXIT_INTERRUPTED = 130


class SummarizedError(RuntimeError):
    """Errors that carry their own one-line user-facing reason.

    Adapter errors (e.g. the GitHub client) subclass this so `error_message`
    can render a concise reason without depending on the adapter module.
    """

    exit_code = EXIT_FAILURE

    def user_facing_reason(self) -> str:
        raise NotImplementedError


def resolve_exit_code(error: BaseException) -> int:
    """Return the process exit code for a raised CLI error.

    A `CliError` subclass with its own category code wins. A generic
    `CliError` wrapping a categorized cause inherits the cause's code, so wrap
    sites do not each need a subclass.
    """

    if not isinstance(error, CliError):
        return EXIT_FAILURE

    current: BaseException | None = error
    while current is not None:
        if isinstance(current, CliError) and current.exit_code != EXIT_FAILURE:
            return current.exit_code
        if isinstance(current, SummarizedError) and current.exit_code != EXIT_FAILURE:
            return current.exit_code
        current = current.__cause__
    return error.exit_code


def error_message(error: BaseException) -> ErrorMessage:
    """Return a user-facing renderable for an exception."""

    if isinstance(error, CliError):
        cause = error.__cause__
        if isinstance(cause, SummarizedError):
            reason = cause.user_facing_reason()
            if plain_text(error.message).strip():
                return (error.message, ": ", reason)
            return reason
        return error.message
    return str(error)


def error_hint(error: BaseException) -> ErrorHint | None:
    """Return the follow-up hint for an exception, if any."""

    if isinstance(error, CliError):
        return error.hint
    return None


class CliError(RuntimeError):
    """Base error for user-facing CLI failures."""

    exit_code = EXIT_FAILURE

    def __init__(self, message: ErrorMessage, *, hint: ErrorHint | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(plain_text(message))

    def __str__(self) -> str:
        message = plain_text(error_message(self)).strip()
        if self.hint is None:
            return message
        hint = plain_text(self.hint).strip()
        if not message:
            return hint
        if not hint:
            return message
        return f"{message} {hint}"


class UsageError(CliError):
    """Invalid command-line arguments or flag combinations."""

    exit_code = EXIT_USAGE


class AmbiguousSelectionError(CliError):
    """A selector matches more than one possible target, so the command fails closed."""

    exit_code = EXIT_AMBIGUOUS


class ConflictedStackError(CliError):
    """Unresolved conflicts in the selected changes block the requested operation."""

    exit_code = EXIT_CONFLICTS


# Which pre-mutation verification check failed when cross-system drift made review
# identity unprovable. The vocabulary matches docs/internals/distributed-state.md so
# fail-closed stops that share an exit code stay distinguishable.
type DriftCondition = Literal[
    "change_unlinked",
    "pull_request_ambiguous",
    "pull_request_not_open",
    "remote_branch_missing",
    "remote_branch_moved",
    "saved_pull_request_mismatch",
    "saved_pull_request_missing",
]


class DriftError(CliError):
    """Cross-system drift left review identity unprovable, so the command fails closed."""

    def __init__(
        self,
        message: ErrorMessage,
        *,
        condition: DriftCondition,
        hint: ErrorHint | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.condition = condition
