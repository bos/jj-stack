"""Validate the observed trunk commit and resolve its GitHub branch."""

from __future__ import annotations

from collections.abc import Sequence

import jj_stack.ui as ui
from jj_stack.errors import CliError, UnsupportedStackError
from jj_stack.github.resolution import resolve_trunk_branch
from jj_stack.identifiers import CommitId
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubRepo
from jj_stack.models.stack import LocalCommit


def require_usable_trunk(trunks: Sequence[LocalCommit]) -> LocalCommit:
    if len(trunks) != 1:
        raise CliError(t"Could not resolve {ui.revset('trunk()')} to one commit.")
    trunk = trunks[0]
    if not trunk.parents:
        raise UnsupportedStackError(
            t"{ui.revset('trunk()')} resolves to the root commit, so this repo has no trunk.",
            hint=t"This usually means the repo has no Git remote, or its trunk branch has not "
            t"been fetched. Run {ui.cmd('jj-stack doctor')} to check the remote and trunk "
            t"branch.",
            reason="trunk_resolved_to_root",
        )
    return trunk


def observe_trunk_branch(
    *,
    jj_client: JjClient,
    github_repo_state: GithubRepo,
    remote: GitRemote,
    trunk_commit_id: CommitId,
) -> tuple[str, dict[str, CommitId]]:
    """Resolve the GitHub base branch from the remote bookmarks jj sees at trunk."""

    return resolve_trunk_branch(
        branches_at_trunk=jj_client.remote_bookmarks_at_commit(
            remote=remote.name, commit_id=trunk_commit_id
        ),
        github_repo_state=github_repo_state,
        remote=remote,
        trunk_commit_id=trunk_commit_id,
    )
