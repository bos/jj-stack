"""User-facing summaries for GitHub client errors."""

from __future__ import annotations

from jj_stack.github.auth import github_token_from_env
from jj_stack.github.client import GithubClientError
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
    UnresolvedGithubTarget,
)
from jj_stack.models.bookmarks import GitRemote
from jj_stack.ui import Message, code


def summarize_github_lookup_error(*, action: str, error: GithubClientError) -> str:
    """Render a concise GitHub lookup failure for `status`-style output."""

    if error.status_code == 401:
        return "GitHub authentication failed - check GITHUB_TOKEN"
    if error.status_code == 403:
        return "GitHub access was denied - check GITHUB_TOKEN and repo access"
    if error.is_repository_not_found():
        message = "GitHub repository not found or inaccessible"
        if github_token_from_env() is None:
            return f"{message} - check GITHUB_TOKEN or gh auth"
        return message
    return f"{action} failed ({error.request_failure_detail()})"


def github_unavailable_message(
    *,
    github_error: Message | None,
    github_repository: GithubRepoAddress | None,
) -> Message | None:
    """Render a concise warning when GitHub-backed work could not proceed."""

    if github_error is None:
        return None
    if github_repository is None:
        return ("GitHub unavailable: ", github_error)
    return ("GitHub unavailable for ", code(github_repository.full_name), ": ", github_error)


def remote_unavailable_message(
    *,
    remote_error: Message | None,
) -> Message:
    """Render a concise warning when Git remote selection could not proceed."""

    if remote_error is None:
        return "No Git remote is configured."
    return remote_error


def github_target_unavailable_messages(
    target: GithubTarget | UnresolvedGithubTarget | None,
) -> tuple[Message, ...]:
    """Render the repo-level warning lines for an unresolved GitHub target."""

    if not isinstance(target, UnresolvedGithubTarget):
        return ()
    return remote_and_github_unavailable_messages(
        github_error=target.github_repository_error,
        github_repository=None,
        remote=target.remote,
        remote_error=target.remote_error,
    )


def remote_and_github_unavailable_messages(
    *,
    github_error: Message | None,
    github_repository: GithubRepoAddress | None,
    remote: GitRemote | None,
    remote_error: Message | None,
) -> tuple[Message, ...]:
    """Render the repo-level warning lines for an unavailable remote or GitHub target."""

    messages: list[Message] = []
    if remote is None:
        messages.append(remote_unavailable_message(remote_error=remote_error))
    github_message = github_unavailable_message(
        github_error=github_error,
        github_repository=github_repository,
    )
    if github_message is not None:
        messages.append(github_message)
    return tuple(messages)

