from __future__ import annotations

import pytest

import jj_stack.github.error_messages as error_messages
from jj_stack.github.client import GithubClientError
from jj_stack.github.error_messages import summarize_github_lookup_error


@pytest.mark.parametrize(
    ("status_code", "token_present", "expected"),
    [
        pytest.param(
            401,
            False,
            "GitHub authentication failed - check GITHUB_TOKEN",
            id="401-auth",
        ),
        pytest.param(
            403,
            False,
            "GitHub access was denied - check GITHUB_TOKEN and repo access",
            id="403-access",
        ),
        pytest.param(
            404,
            False,
            "GitHub repository not found or inaccessible - check GITHUB_TOKEN or gh auth",
            id="404-without-token",
        ),
        pytest.param(
            404,
            True,
            "GitHub repository not found or inaccessible",
            id="404-with-token",
        ),
        pytest.param(
            502,
            False,
            "pull request lookup failed (GitHub 502)",
            id="other-status-reports-action",
        ),
    ],
)
def test_summarize_github_lookup_error_maps_failures_to_actionable_hints(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    token_present: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(
        error_messages,
        "github_token_from_env",
        lambda: "token" if token_present else None,
    )

    summary = summarize_github_lookup_error(
        action="pull request lookup",
        error=GithubClientError("GitHub request failed: boom", status_code=status_code),
    )

    assert summary == expected
