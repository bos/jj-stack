"""User-facing summaries for GitHub client errors."""

from __future__ import annotations

from collections.abc import Awaitable

from jj_stack.errors import CliError
from jj_stack.github.client import REPO_NOT_FOUND_REASON, GithubClient, GithubClientError
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
    UnresolvedGithubTarget,
)
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubRepo
from jj_stack.ui import Message, code


def github_action_error_message(*, action: str, error: GithubClientError) -> str:
    """Prefix the client's canonical GitHub failure reason with its failed action."""

    return f"{action}: {error.user_facing_reason()}"


def repo_lookup_reason(error: GithubClientError) -> str:
    """Explain a failed lookup of the repo itself, where a 404 means the repo is out of reach."""

    if error.status_code == 404:
        return REPO_NOT_FOUND_REASON
    return error.user_facing_reason()


def repo_lookup_error(
    error: GithubClientError,
    *,
    repo: str,
    hint: Message | None = None,
) -> CliError:
    """Wrap a failed repo lookup; raise the result `from error`."""

    if error.status_code == 404:
        hint = REPO_NOT_FOUND_REASON
    return CliError(("Could not inspect GitHub repo ", code(repo)), hint=hint)


async def read_or_stop[T](
    read: Awaitable[T], *, message: Message, hint: Message | None = None
) -> T:
    """Await one GitHub read, turning a client failure into a stop with `message`."""

    try:
        return await read
    except GithubClientError as error:
        raise CliError(message, hint=hint) from error


async def observe_github_repo(github: GithubClient, *, hint: Message | None = None) -> GithubRepo:
    try:
        return await github.get_repo()
    except GithubClientError as error:
        raise repo_lookup_error(error, repo=github.repo.full_name, hint=hint) from error


def github_unavailable_message(
    *,
    github_error: Message | None,
    github_repo: GithubRepoAddress | None,
) -> Message | None:
    """Render a concise warning when GitHub-backed work could not proceed."""

    if github_error is None:
        return None
    if github_repo is None:
        return ("GitHub unavailable: ", github_error)
    return ("GitHub unavailable for ", code(github_repo.full_name), ": ", github_error)


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
        github_error=target.github_repo_error,
        github_repo=None,
        remote=target.remote,
        remote_error=target.remote_error,
    )


def remote_and_github_unavailable_messages(
    *,
    github_error: Message | None,
    github_repo: GithubRepoAddress | None,
    remote: GitRemote | None,
    remote_error: Message | None,
) -> tuple[Message, ...]:
    """Render the repo-level warning lines for an unavailable remote or GitHub target."""

    messages: list[Message] = []
    if remote is None:
        messages.append(remote_unavailable_message(remote_error=remote_error))
    github_message = github_unavailable_message(
        github_error=github_error,
        github_repo=github_repo,
    )
    if github_message is not None:
        messages.append(github_message)
    return tuple(messages)
