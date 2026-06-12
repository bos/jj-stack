from __future__ import annotations

from jj_stack.errors import CliError, error_message
from jj_stack.github.client import GithubClientError
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
    monkeypatch.setattr("jj_stack.github.error_messages.github_token_from_env", lambda: "token")

    cause = GithubClientError(
        'GitHub request failed: 404 {"message":"Not Found"}',
        status_code=404,
    )
    try:
        raise CliError("") from cause
    except CliError as error:
        assert plain_text(error_message(error)) == "repo not found or inaccessible"
