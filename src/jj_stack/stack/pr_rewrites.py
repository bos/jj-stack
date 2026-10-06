"""Decide whether a PR head that moved still holds only the submitted change.

GitHub rewrites PR heads itself when it merges part of a GitHub stack or rebases one. Those
commits stay on GitHub: they are read for comparison but never become local commits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.error_messages import read_or_stop
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.change_state import ChangeObservation


@dataclass(frozen=True, slots=True)
class PRHead:
    """A PR's observed head with the commits of its change that this repo holds."""

    head: CommitId
    submitted: CommitId
    local: CommitId | None

    @classmethod
    def of(cls, tracked: TrackedPR, pr: GithubPR, selected: LocalCommit | None) -> PRHead:
        return cls(
            pr.head.sha,
            tracked.submitted_baseline.commit_id,
            selected.commit_id if selected is not None else None,
        )

    @property
    def moved(self) -> bool:
        return self.head not in (self.submitted, self.local)


@dataclass(frozen=True, slots=True)
class HeadRewrite:
    # The head's only parent; None when it has several.
    parent: CommitId | None
    # The submitted change applied to `parent` gives exactly the head's tree.
    same_content: bool
    parent_on_trunk: bool


async def find_head_rewrites(
    jj_client: JjClient,
    github: GithubClient,
    *,
    remote: str,
    trunk_branch: str,
    chain: Sequence[ChangeObservation],
) -> dict[ChangeId, CommitId | None]:
    """Map each change with a moved PR head to that head's parent if it may be overwritten.

    A head that may not be overwritten maps to None. `chain` lists one local stack's changes
    from the bottom. Merged PRs are left out, so the lowest open PR is judged against trunk,
    where GitHub roots its rewrites. A failed read of GitHub's trunk stops the command.
    """

    heads = {
        item.change_id: PRHead.of(item.tracked, item.pr, item.selected)
        for item in chain
        if item.tracked is not None
        and isinstance(item.pr, GithubPR)
        and item.pr.state != "merged"
    }
    moved = tuple(head.head for head in heads.values() if head.moved)
    if not moved:
        return {}
    # Fetch the moved heads while GitHub reports its trunk; their history usually includes it.
    fetch = asyncio.create_task(
        asyncio.to_thread(_fetch_missing, jj_client, remote=remote, commit_ids=moved)
    )
    unreadable = t"Could not inspect trunk branch {ui.bookmark(trunk_branch)} on GitHub."
    try:
        # GitHub's trunk, which may be ahead of the local one.
        targets = await read_or_stop(
            github.get_branch_targets(branches=(trunk_branch,)), message=unreadable
        )
        if (tip := targets.get(trunk_branch)) is None:
            raise CliError(unreadable)
    except BaseException:
        # Cancelling to_thread cannot stop the git subprocess, so join it first.
        await asyncio.gather(asyncio.shield(fetch), return_exceptions=True)
        raise
    await fetch
    rewrites = _observe_head_rewrites(
        jj_client, remote=remote, trunk_commit_id=tip, heads=tuple(heads.values())
    )
    return {
        change_id: rewrites[head.head].parent if replaceable else None
        for (change_id, head), replaceable in zip(
            heads.items(), _replaceable_heads(tuple(heads.values()), rewrites), strict=True
        )
        if head.moved
    }


def _fetch_missing(jj_client: JjClient, *, remote: str, commit_ids: Sequence[CommitId]) -> None:
    try:
        jj_client.git_tree_ids(commit_ids)
    except JjCommandError:
        jj_client.fetch_commits(remote=remote, commit_ids=commit_ids)


def _observe_head_rewrites(
    jj_client: JjClient,
    *,
    remote: str,
    trunk_commit_id: CommitId,
    heads: Sequence[PRHead],
) -> dict[CommitId, HeadRewrite]:
    """Compare each moved head with its submitted commit, keyed by head."""

    moved = tuple(item for item in heads if item.moved)
    # Fetches GitHub's trunk only when the moved heads did not bring it along.
    jj_client.read_remote_git_commit(remote=remote, commit_id=trunk_commit_id)
    trees = jj_client.git_tree_ids(tuple(item.head for item in moved))
    rewrites: dict[CommitId, HeadRewrite] = {}
    for item in moved:
        parents = jj_client.read_remote_git_commit(remote=remote, commit_id=item.head).parents
        submitted = jj_client.read_remote_git_commit(remote=remote, commit_id=item.submitted)
        if len(parents) != 1 or len(submitted.parents) != 1:
            rewrites[item.head] = HeadRewrite(None, same_content=False, parent_on_trunk=False)
            continue
        tree = jj_client.rebased_tree(
            item.submitted, parent=submitted.parents[0], onto=parents[0]
        )
        rewrites[item.head] = HeadRewrite(
            parents[0],
            same_content=tree == trees[item.head],
            parent_on_trunk=jj_client.on_first_parent_chain(parents[0], tip=trunk_commit_id),
        )
    return rewrites


def _replaceable_heads(
    chain: Sequence[PRHead], rewrites: dict[CommitId, HeadRewrite]
) -> tuple[bool, ...]:
    """Whether each PR head, bottom first, may be overwritten with the local commit.

    A head qualifies when it is the submitted or local commit, or a rewrite of the submitted
    commit with the same content: rooted on trunk's first-parent history for the bottom PR,
    and on the PR below otherwise. Anything else is someone else's work.
    """

    result: list[bool] = []
    below: tuple[CommitId | None, ...] = ()
    for item in chain:
        rewrite = rewrites.get(item.head)
        result.append(
            not item.moved
            or (
                rewrite is not None
                and rewrite.same_content
                and (rewrite.parent in below if below else rewrite.parent_on_trunk)
            )
        )
        below = (item.head, item.submitted, item.local)
    return tuple(result)
