"""Shared Git remote and GitHub target resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import jj_stack.ui as ui
from jj_stack.errors import CliError, ErrorMessage, error_message
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubRepository
from jj_stack.review.branches import is_review_branch

if TYPE_CHECKING:
    from jj_stack.jj.client import JjClient


@dataclass(frozen=True, slots=True)
class GithubRepoAddress:
    """GitHub repository coordinates parsed from a Git remote URL."""

    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def repository_key(self) -> tuple[str, str]:
        """Return the case-insensitive nominal repository identity."""

        return self.owner.casefold(), self.repo.casefold()


@dataclass(frozen=True, slots=True)
class GithubTarget:
    """A fully resolved GitHub target: the selected Git remote and its repository."""

    remote: GitRemote
    repository: GithubRepoAddress

    # A resolved target carries no diagnostics. These mirror UnresolvedGithubTarget so
    # degraded-mode consumers can read errors off either arm without narrowing.
    @property
    def remote_error(self) -> None:
        return None

    @property
    def github_repository_error(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class UnresolvedGithubTarget:
    """A GitHub target that could not be fully resolved.

    Encodes three degraded states:

    - no Git remotes exist at all: every field is None
    - remote selection failed: only `remote_error` is set
    - a remote resolved but is not a GitHub remote: `remote` and
      `github_repository_error` are set
    """

    remote: GitRemote | None = None
    remote_error: ErrorMessage | None = None
    github_repository_error: ErrorMessage | None = None


def select_submit_remote(remotes: tuple[GitRemote, ...]) -> GitRemote:
    """Resolve the Git remote used by review commands."""

    remotes_by_name = {remote.name: remote for remote in remotes}
    if "origin" in remotes_by_name:
        return remotes_by_name["origin"]
    if len(remotes) == 1:
        return remotes[0]
    raise CliError(
        "Could not determine which Git remote to use.",
        hint=t"Add an {ui.bookmark('origin')} remote or leave exactly one remote.",
    )


def parse_github_repo(remote: GitRemote) -> GithubRepoAddress | None:
    """Parse one GitHub repository target from a Git remote's URLs."""

    fetch_repository = _parse_github_url(remote.fetch_url)
    push_repository = _parse_github_url(remote.push_url)
    if fetch_repository != push_repository:
        return None
    return fetch_repository


def _parse_github_url(remote_url: str) -> GithubRepoAddress | None:
    """Parse a GitHub repository target from one Git remote URL."""

    parsed = urlparse(remote_url)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
        raw_path = parsed.path
    elif _looks_like_scp_remote(remote_url):
        _, _, raw_path = remote_url.partition(":")
    else:
        return None

    normalized_path = raw_path.lstrip("/").removesuffix(".git")
    parts = [part for part in normalized_path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    return GithubRepoAddress(owner=owner, repo=repo)


def _looks_like_scp_remote(url: str) -> bool:
    """Return whether a remote uses Git's scp-style host:path shorthand."""

    prefix, separator, suffix = url.partition(":")
    if not separator or not prefix or not suffix:
        return False
    if "/" in prefix or "\\" in prefix:
        return False
    # Reject Windows drive paths like C:/repo.git.
    if len(prefix) == 1 and prefix.isalpha():
        return False
    return True


def resolve_github_target(
    remotes: tuple[GitRemote, ...],
) -> GithubTarget | UnresolvedGithubTarget:
    """Resolve the optional remote/GitHub target used by read-mostly commands."""

    if not remotes:
        return UnresolvedGithubTarget()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        return UnresolvedGithubTarget(remote_error=error_message(error))

    github_repository = parse_github_repo(remote)
    if github_repository is None:
        return UnresolvedGithubTarget(
            remote=remote,
            github_repository_error=(
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(remote.name)}. Its fetch and push URLs must identify "
                t"the same GitHub repository."
            ),
        )
    return GithubTarget(remote=remote, repository=github_repository)


def require_github_repo(remote: GitRemote) -> GithubRepoAddress:
    """Parse a GitHub repository target or raise a user-facing CLI error."""

    github_repository = parse_github_repo(remote)
    if github_repository is not None:
        return github_repository
    raise CliError(
        t"Could not determine the GitHub repository for remote {ui.bookmark(remote.name)}.",
        hint="Ensure its fetch and push URLs identify the same GitHub repository.",
    )


def resolve_trunk_branch(
    *,
    client: JjClient,
    github_repository_state: GithubRepository,
    remote: GitRemote,
    trunk_commit_id: str,
) -> tuple[str, dict[str, str]]:
    """Resolve the GitHub base branch used for bottom-of-stack pull requests."""

    remote_targets = {
        branch: target
        for branch, target in client.list_remote_branches(
            remote=remote.name,
            patterns=("refs/heads/*",),
        ).items()
        if not is_review_branch(branch)
    }
    matches = tuple(
        branch for branch, target in remote_targets.items() if target == trunk_commit_id
    )
    default_branch = github_repository_state.default_branch
    if default_branch:
        # No match at all usually just means trunk() is behind the remote, which is fine.
        # A match on some *other* branch is positive evidence that GitHub's default branch
        # is not the branch jj calls trunk, and basing pull requests on it would be wrong.
        if matches and default_branch not in matches:
            raise CliError(
                t"GitHub's default branch for {ui.bookmark(remote.name)} is "
                t"{ui.bookmark(default_branch)}, but {ui.revset('trunk()')} is "
                t"{ui.join(ui.bookmark, matches)}.",
                hint=(
                    t"Point {ui.revset('trunk()')} at {ui.bookmark(default_branch)}, or change "
                    t"the repository's default branch on GitHub, so pull requests are based on "
                    t"the branch you review against."
                ),
            )
        return default_branch, remote_targets
    if len(matches) == 1:
        return matches[0], remote_targets
    if len(matches) > 1:
        raise CliError(
            t"Could not determine the trunk branch because multiple remote branches on "
            t"{ui.bookmark(remote.name)} point at {ui.revset('trunk()')}: "
            t"{ui.join(ui.bookmark, matches)}."
        )
    raise CliError(
        t"Could not determine the trunk branch for remote {ui.bookmark(remote.name)}.",
        hint=(
            t"Ensure the GitHub repository exposes a default branch or create one "
            t"remote branch that points at {ui.revset('trunk()')}."
        ),
    )
