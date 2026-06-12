"""Shared Git remote and GitHub target resolution helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

import jj_stack.ui as ui
from jj_stack.errors import CliError, ErrorMessage, error_message
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.github import GithubRepository


@dataclass(frozen=True, slots=True)
class GithubRepoAddress:
    """GitHub repository coordinates parsed from a Git remote URL."""

    host: str
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


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
    """Parse a GitHub repository target from a Git remote URL."""

    parsed = urlparse(remote.url)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
        host = parsed.hostname
        raw_path = parsed.path
    elif _looks_like_scp_remote(remote.url):
        host, _, raw_path = remote.url.partition(":")
        host = host.rsplit("@", maxsplit=1)[-1]
    else:
        return None

    normalized_path = raw_path.lstrip("/").removesuffix(".git")
    parts = [part for part in normalized_path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    return GithubRepoAddress(host=host, owner=owner, repo=repo)


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
                t"{ui.bookmark(remote.name)}. Use a GitHub remote URL."
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
        hint="Use a GitHub remote URL.",
    )


def resolve_trunk_branch(
    *,
    bookmark_states: Mapping[str, BookmarkState],
    github_repository_state: GithubRepository,
    remote_name: str,
    trunk_commit_id: str,
) -> str:
    """Resolve the GitHub base branch used for bottom-of-stack pull requests."""

    if github_repository_state.default_branch:
        return github_repository_state.default_branch

    remote_bookmarks = remote_bookmarks_pointing_at_commit(
        bookmark_states=bookmark_states,
        remote_name=remote_name,
        commit_id=trunk_commit_id,
    )
    if len(remote_bookmarks) == 1:
        return remote_bookmarks[0]
    if len(remote_bookmarks) > 1:
        raise CliError(
            t"Could not determine the trunk branch because multiple remote bookmarks on "
            t"{ui.bookmark(remote_name)} point at {ui.revset('trunk()')}: "
            t"{ui.join(ui.bookmark, remote_bookmarks)}."
        )
    raise CliError(
        t"Could not determine the trunk branch for remote {ui.bookmark(remote_name)}.",
        hint=(
            t"Ensure the GitHub repository exposes a default branch or create one "
            t"remote bookmark that points at {ui.revset('trunk()')}."
        ),
    )


def remote_bookmarks_pointing_at_commit(
    *,
    bookmark_states: Mapping[str, BookmarkState],
    remote_name: str,
    commit_id: str,
) -> tuple[str, ...]:
    matches = [
        name
        for name, bookmark_state in bookmark_states.items()
        if (remote_state := bookmark_state.remote_target(remote_name)) is not None
        and remote_state.target == commit_id
    ]
    return tuple(sorted(matches))
