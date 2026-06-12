"""User-facing summaries for GitHub client errors."""

from __future__ import annotations

from jj_stack.github.auth import github_token_from_env
from jj_stack.github.client import GithubClientError
from jj_stack.ui import Message, code


def summarize_github_error_reason(error: GithubClientError) -> str:
    """Render a concise GitHub failure reason suitable after an action prefix."""

    if error.status_code == 401:
        return "auth failed - check GITHUB_TOKEN"
    if error.status_code == 403:
        return "access denied - check GITHUB_TOKEN and repo access"
    if is_repository_not_found_error(error):
        return _github_auth_failure_message("repo not found or inaccessible")
    return f"request failed ({_request_failure_detail(error)})"


def summarize_github_lookup_error(*, action: str, error: GithubClientError) -> str:
    """Render a concise GitHub lookup failure for `status`-style output."""

    if error.status_code == 401:
        return "GitHub authentication failed - check GITHUB_TOKEN"
    if error.status_code == 403:
        return "GitHub access was denied - check GITHUB_TOKEN and repo access"
    if is_repository_not_found_error(error):
        return _github_auth_failure_message("GitHub repository not found or inaccessible")
    return f"{action} failed ({_request_failure_detail(error)})"


def github_unavailable_message(
    *,
    github_error: Message | None,
    github_repository: str | None,
) -> Message | None:
    """Render a concise warning when GitHub-backed work could not proceed."""

    if github_error is None:
        return None
    if github_repository is None:
        return ("GitHub unavailable: ", github_error)
    return ("GitHub unavailable for ", code(github_repository), ": ", github_error)


def remote_unavailable_message(
    *,
    remote_error: Message | None,
) -> Message:
    """Render a concise warning when Git remote selection could not proceed."""

    if remote_error is None:
        return "No Git remote is configured."
    return remote_error


def github_error_detail(error: GithubClientError) -> str:
    """Return a concise detail string from a GitHub client error."""

    message = str(error).strip()
    for prefix in (
        "GitHub request failed: ",
        "GitHub pull request head lookup failed: ",
        "GitHub pull request batch lookup failed: ",
        "GitHub pull request review decision lookup failed: ",
        "GitHub issue comment list failed: ",
    ):
        if message.startswith(prefix):
            return message.removeprefix(prefix).strip()
    return message


def is_repository_not_found_error(error: GithubClientError) -> bool:
    """Return whether the error indicates the repository is missing or inaccessible."""

    detail = github_error_detail(error)
    if "Could not resolve to a Repository with the name" in detail:
        return True
    return error.status_code == 404


def _request_failure_detail(error: GithubClientError) -> str:
    if error.status_code is None:
        return github_error_detail(error)
    return f"GitHub {error.status_code}"


def _github_auth_failure_message(message: str) -> str:
    if github_token_from_env() is None:
        return f"{message} - check GITHUB_TOKEN or gh auth"
    return message
