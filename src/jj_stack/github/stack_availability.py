"""Shared GitHub Stacks API availability diagnostics."""

from jj_stack.errors import EXIT_GITHUB, CliError
from jj_stack.github.client import GithubClientError

GITHUB_STACKS_ROLLOUT_URL = "https://gh.io/stacksbeta"


class GithubStacksUnavailableError(CliError):
    """The repository cannot use GitHub's Stacks API."""

    exit_code = EXIT_GITHUB


def github_stacks_unavailable_error(
    *,
    error: GithubClientError,
    repository: str,
) -> GithubStacksUnavailableError | None:
    """Explain a missing Stacks API after repository access has succeeded."""

    if error.status_code != 404:
        return None
    return GithubStacksUnavailableError(
        f"GitHub stacked pull requests are unavailable for {repository}.",
        hint=(
            "See GitHub's current availability and requirements at "
            f"{GITHUB_STACKS_ROLLOUT_URL}, then rerun the command."
        ),
    )
