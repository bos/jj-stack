"""Read commits and tracking to find local stacks."""

from __future__ import annotations

from collections.abc import Sequence

from jj_stack.identifiers import CommitId
from jj_stack.jj.client import JjClient, change_ids_revset, quote_revset_symbol
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.observation import TRUNK_PATH, observe_stack_commits
from jj_stack.stack.path import (
    RepoPathObservation,
    RepoStackPaths,
    project_repo_paths,
)
from jj_stack.stack.trunk import require_usable_trunk


def observe_repo_paths(
    *,
    jj_client: JjClient,
    state: TrackingState,
    descendant_of: Sequence[CommitId] = (),
) -> RepoStackPaths:
    """Read the commits needed to find local stacks in one batch.

    With no descendant_of IDs, inspect the paths through every tracked change. Otherwise,
    inspect descendants of those commits. Callers keep only the stacks containing their
    requested commit.
    """

    if descendant_of:
        anchors = " | ".join(quote_revset_symbol(commit_id) for commit_id in descendant_of)
        visible_scope = f"(visible() & ({anchors})::)"
    elif state.prs:
        # A stack path is a first-parent chain through a tracked change, so only the tracked
        # changes' descendants and first-parent ancestors can belong to one. `change_id()`
        # resolves visible commits only, so no `visible()` filter is needed here.
        tracked = change_ids_revset(tuple(state.prs))
        visible_scope = f"({tracked}:: | first_ancestors({tracked}))"
    else:
        visible_scope = "none()"
    candidates = f"({visible_scope} ~ {TRUNK_PATH})"
    rows = observe_stack_commits(
        jj_client=jj_client,
        state=state,
        revset=f"trunk() | ancestors({candidates}, 2) | @",
        membership_revsets=("trunk()", candidates, TRUNK_PATH),
    ).rows
    trunks = tuple(commit for commit, flags in rows if flags[0])
    trunk = require_usable_trunk(trunks)
    current_working_copy = next(
        (commit for commit, _flags in rows if commit.current_working_copy),
        None,
    )
    current_tracked_commit_id = (
        (
            current_working_copy.commit_id
            if current_working_copy.change_id in state.prs
            and current_working_copy.has_described_work
            else current_working_copy.parents[0]
        )
        if current_working_copy is not None and len(current_working_copy.parents) == 1
        else None
    )
    return project_repo_paths(
        RepoPathObservation(
            candidate_commit_ids=frozenset(
                commit.commit_id for commit, flags in rows if flags[1]
            ),
            current_tracked_commit_id=current_tracked_commit_id,
            trunk_first_parent_ids=frozenset(
                commit.commit_id for commit, flags in rows if flags[2]
            ),
            commits=tuple(commit for commit, _flags in rows),
            tracked_change_ids=frozenset(state.prs),
            trunk=trunk,
        )
    )
