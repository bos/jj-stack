from __future__ import annotations

from jj_stack.errors import EXIT_GITHUB, CliError, resolve_exit_code
from jj_stack.github.client import REPO_NOT_FOUND_REASON, GithubClientError
from jj_stack.github.error_messages import repo_lookup_error, repo_lookup_reason
from jj_stack.ui import plain_text


def test_repo_lookup_treats_a_404_as_a_missing_or_hidden_repo() -> None:
    """Only a lookup of the repo itself may read a 404 as "no such repo for this token"."""

    missing = GithubClientError(
        "GitHub request failed: 404", body='{"message":"Not Found"}', status_code=404
    )
    refused = GithubClientError("GitHub request failed: 500", status_code=500)

    assert repo_lookup_reason(missing) == REPO_NOT_FOUND_REASON
    assert repo_lookup_reason(refused) == "request failed (GitHub 500)"

    wrapped = repo_lookup_error(missing, repo="octo-org/repo", hint="rerun later")
    assert plain_text(wrapped.message) == "Could not inspect GitHub repo octo-org/repo"
    assert wrapped.hint == REPO_NOT_FOUND_REASON
    assert repo_lookup_error(refused, repo="octo-org/repo", hint="rerun later").hint == (
        "rerun later"
    )
    try:
        raise wrapped from missing
    except CliError as error:
        assert resolve_exit_code(error) == EXIT_GITHUB
