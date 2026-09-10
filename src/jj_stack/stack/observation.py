"""Interpret fetched PR bookmarks at the stack observation boundary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import batched

from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import QUERY_BATCH_SIZE, JjClient, change_ids_revset, quote_revset_symbol
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackingState
from jj_stack.pr_branch_namespace import current_pr_branch_namespace

TRUNK_PATH = "first_ancestors(trunk())"


@dataclass(frozen=True, slots=True)
class StackObservation:
    """Interpreted commit rows and the explicit jj config used to observe them."""

    rows: tuple[tuple[LocalCommit, tuple[bool, ...]], ...]
    cli_args: JjCliArgs

    def copies(
        self, change_ids: Sequence[ChangeId], *, off_trunk: bool = False
    ) -> dict[ChangeId, tuple[LocalCommit, ...]]:
        grouped: dict[ChangeId, list[LocalCommit]] = {change_id: [] for change_id in change_ids}
        for commit, flags in self.rows:
            if not off_trunk or flags[0]:
                grouped[commit.change_id].append(commit)
        return {change_id: tuple(commits) for change_id, commits in grouped.items()}


def observe_stack_commits(
    *,
    jj_client: JjClient,
    state: TrackingState,
    revset: str,
    membership_revsets: Sequence[str] = (),
    selected_revset: str | None = None,
) -> StackObservation:
    """Observe raw copies together, then distinguish a saved snapshot from its local rewrite."""

    cli_args, expected = observe_pr_bookmarks(jj_client=jj_client, state=state)
    rows_by_commit: dict[CommitId, tuple[LocalCommit, tuple[bool, ...]]] = {}
    scopes = tuple(batched(tuple(expected), QUERY_BATCH_SIZE, strict=False)) or ((),)
    for index, scope in enumerate(scopes):
        copies = change_ids_revset(scope) if scope else "none()"
        rows = jj_client.query_commits_with_membership(
            f"({revset}) | ({copies})" if index == 0 else copies,
            membership_revsets=(revset, *membership_revsets),
            selected_revset=selected_revset or revset,
            cli_args=cli_args,
        )
        rows_by_commit.update((commit.commit_id, (commit, flags)) for commit, flags in rows)
    return StackObservation(
        rows=tuple(
            (commit, flags[1:])
            for commit, flags in _project_copies(tuple(rows_by_commit.values()), expected)
            if flags[0]
        ),
        cli_args=cli_args,
    )


def observe_change_copies(
    *, jj_client: JjClient, state: TrackingState, change_ids: Sequence[ChangeId]
) -> StackObservation:
    """Read all copies of the requested changes in batches, reading bookmarks once."""

    cli_args, expected = observe_pr_bookmarks(jj_client=jj_client, state=state)
    rows = tuple(
        row
        for scope in batched(tuple(dict.fromkeys(change_ids)), QUERY_BATCH_SIZE, strict=False)
        for row in jj_client.query_commits_with_membership(
            change_ids_revset(scope),
            membership_revsets=(f"~{TRUNK_PATH}",),
            cli_args=cli_args,
        )
    )
    return StackObservation(rows=_project_copies(rows, expected), cli_args=cli_args)


def _project_copies(
    rows: tuple[tuple[LocalCommit, tuple[bool, ...]], ...], expected: Mapping[ChangeId, CommitId]
) -> tuple[tuple[LocalCommit, tuple[bool, ...]], ...]:
    grouped: dict[ChangeId, list[LocalCommit]] = {}
    for commit, _flags in rows:
        if not commit.hidden:
            grouped.setdefault(commit.change_id, []).append(commit)
    snapshots = {
        baseline
        for change_id, baseline in expected.items()
        if len(matches := grouped.get(change_id, ())) == 2
        and any(commit.commit_id == baseline for commit in matches)
        and all(not commit.immutable for commit in matches)
    }
    return tuple(
        (
            commit.model_copy(update={"divergent": False})
            if expected.get(commit.change_id) in snapshots
            else commit,
            flags,
        )
        for commit, flags in rows
        if commit.commit_id not in snapshots
    )


def observe_pr_bookmarks(
    *, jj_client: JjClient, state: TrackingState
) -> tuple[JjCliArgs, dict[ChangeId, CommitId]]:
    """Narrow built-in remote immutability using one complete bookmark observation.

    A saved branch must have one owner and exactly its baseline as target. Untracked remote
    bookmarks may also permit adoption of nondivergent namespace commits. Shared targets,
    trunk, tags, and user additions to immutable_heads() retain their protection.
    """

    namespace = current_pr_branch_namespace()
    bookmarks = jj_client.query_bookmarks()
    targets: dict[str, set[CommitId]] = {}
    untracked = [
        (row.name, target)
        for row in bookmarks
        if row.remote is not None and not row.tracked
        for target in row.target
    ]
    for row in bookmarks:
        targets.setdefault(row.name, set()).update(row.target)
    matching = {
        change_id: item
        for change_id, item in sorted(state.prs.items())
        if namespace.contains(branch := item.pr_identity.head_ref)
        and targets.get(branch) == {item.submitted_baseline.commit_id}
    }
    claims = Counter(item.pr_identity.head_ref for item in matching.values())
    exact = {
        change_id: item
        for change_id, item in matching.items()
        if claims[item.pr_identity.head_ref] == 1
    }
    accepted = [
        f"(remote_bookmarks(exact:{quote_revset_symbol(item.pr_identity.head_ref)}) & "
        f"{quote_revset_symbol(item.submitted_baseline.commit_id)})"
        for item in exact.values()
        if (item.pr_identity.head_ref, item.submitted_baseline.commit_id) in untracked
    ]
    if any(namespace.contains(name) for name, _target in untracked):
        accepted.append(
            f"(untracked_remote_bookmarks(glob:{quote_revset_symbol(namespace.branch_glob)})"
            " ~ divergent())"
        )
    shared = (
        " | ".join(
            quote_revset_symbol(commit_id)
            for commit_id, count in Counter(target for _name, target in untracked).items()
            if count > 1
        )
        or "none()"
    )
    selectors = " | ".join(accepted) or "none()"
    immutable = (
        f"trunk() | tags() | (untracked_remote_bookmarks() ~ (({selectors}) ~ ({shared})))"
    )
    return (
        JjCliArgs(argv=("--config", f'revset-aliases."builtin_immutable_heads()"={immutable}'))
        if accepted
        else JjCliArgs(),
        {change_id: item.submitted_baseline.commit_id for change_id, item in exact.items()},
    )
