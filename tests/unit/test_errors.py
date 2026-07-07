from __future__ import annotations

from jj_stack.errors import (
    EXIT_AMBIGUOUS,
    EXIT_FAILURE,
    EXIT_GITHUB,
    EXIT_NO_STACK,
    EXIT_USAGE,
    AmbiguousSelectionError,
    CliError,
    UsageError,
    error_message,
    resolve_exit_code,
)
from jj_stack.github.client import GithubClientError
from jj_stack.jj.client import UnsupportedStackError
from jj_stack.ui import plain_text


def test_error_message_appends_github_cause_reason() -> None:
    cause = GithubClientError("Connection refused")
    try:
        raise CliError("Could not load pull request #7") from cause
    except CliError as error:
        assert (
            plain_text(error_message(error))
            == "Could not load pull request #7: request failed (Connection refused)"
        )
        assert (
            str(error)
            == "Could not load pull request #7: request failed (Connection refused)"
        )


def test_error_message_uses_github_cause_reason_when_message_is_empty(
    monkeypatch,
) -> None:
    monkeypatch.setattr("jj_stack.github.client.github_token_from_env", lambda: "token")

    cause = GithubClientError(
        'GitHub request failed: 404 {"message":"Not Found"}',
        status_code=404,
    )
    try:
        raise CliError("") from cause
    except CliError as error:
        assert plain_text(error_message(error)) == "repo not found or inaccessible"


def test_resolve_exit_code_prefers_the_error_category_over_the_cause() -> None:
    generic = CliError("failed")
    assert resolve_exit_code(generic) == EXIT_FAILURE

    categorized = UsageError("bad flag")
    assert resolve_exit_code(categorized) == EXIT_USAGE

    stack_error = UnsupportedStackError("merge commits are not supported")
    assert resolve_exit_code(stack_error) == EXIT_NO_STACK

    # A categorized error keeps its own code even with a GitHub cause.
    try:
        raise UnsupportedStackError("unsupported") from GithubClientError("boom")
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_NO_STACK


def test_resolve_exit_code_inherits_github_code_from_wrapped_cause() -> None:
    try:
        raise CliError("Could not load pull request #7") from GithubClientError("boom")
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_GITHUB

    assert resolve_exit_code(RuntimeError("not a cli error")) == EXIT_FAILURE


def test_resolve_exit_code_inherits_cli_code_from_wrapped_cause() -> None:
    try:
        raise CliError("Local history does not form a linear stack.") from UnsupportedStackError(
            "merge commits are not supported"
        )
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_NO_STACK

    try:
        raise CliError("Could not resolve selector.") from AmbiguousSelectionError(
            "selector matched more than one target"
        )
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_AMBIGUOUS
