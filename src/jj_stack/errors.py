"""User-facing error types shared across CLI commands."""

from __future__ import annotations

from jj_stack.ui import Message, plain_text

type ErrorMessage = Message
type ErrorHint = Message


class SummarizedError(RuntimeError):
    """Errors that carry their own one-line user-facing reason.

    Adapter errors (e.g. the GitHub client) subclass this so `error_message`
    can render a concise reason without depending on the adapter module.
    """

    def user_facing_reason(self) -> str:
        raise NotImplementedError


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

    exit_code = 1

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
