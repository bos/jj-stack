"""Observe ordinary repo paths for the pure path projection."""

from __future__ import annotations

import json
from collections.abc import Sequence

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.path import (
    RepoPathObservation,
    RepoStackPaths,
    project_repo_paths,
)
from jj_stack.stack.pr_branches import prepare_visible_pr_snapshots


def observe_repo_paths(
    *,
    jj_client: JjClient,
    state: TrackingState,
    descendant_of: Sequence[str] = (),
    exclude_trunk_descendants: bool = False,
    include_working_copies: bool = False,
) -> RepoStackPaths:
    """Batch the visible facts for ordinary maximal paths.

    With no anchors this observes the repo inventory. Exact commit anchors
    narrow the observation to paths descending from those changes.
    """

    trunk_path = "first_ancestors(trunk())"
    visible_scope = "visible()"
    if descendant_of:
        anchors = " | ".join(json.dumps(commit_id) for commit_id in descendant_of)
        descendants = f"({anchors})::"
        if exclude_trunk_descendants:
            descendants += " ~ trunk()::"
        visible_scope = f"(visible() & {descendants})"
    candidates = f"(({visible_scope}) ~ {trunk_path} ~ working_copies())"
    if state.pr_identities:
        tracked = " | ".join(
            f"change_id({json.dumps(change_id)})" for change_id in sorted(state.pr_identities)
        )
        candidates = f"({candidates} | ({visible_scope} & working_copies() & ({tracked})))"
    if include_working_copies:
        if not descendant_of:
            raise ValueError("Working-copy dependency observation requires an exact ancestor.")
        candidates = f"({candidates} | (working_copies() & {visible_scope}))"
    prepare_visible_pr_snapshots(jj_client=jj_client, state=state)
    rows = jj_client.query_commits_with_membership(
        f"trunk() | ({candidates}) | parents({candidates}) | @",
        membership_revsets=("trunk()", candidates, trunk_path),
    )
    trunks = tuple(commit for commit, flags in rows if flags[0])
    if len(trunks) != 1:
        raise CliError(t"Could not resolve {ui.revset('trunk()')} to one commit.")
    trunk = trunks[0]
    if not trunk.parents:
        raise UnsupportedStackError(
            t"No trunk bookmark is configured for this repo.",
            hint=t"Create a trunk bookmark such as {ui.bookmark('main')}, then retry.",
            reason="trunk_resolved_to_root",
        )
    current_working_copy = next(
        (commit for commit, _flags in rows if commit.current_working_copy),
        None,
    )
    current_tracked_commit_id = (
        (
            current_working_copy.commit_id
            if current_working_copy.change_id in state.pr_identities
            and not current_working_copy.empty
            and bool(current_working_copy.description.strip())
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
            fetched_trunk_commit_ids=frozenset(
                commit.commit_id for commit, flags in rows if flags[2]
            ),
            commits=tuple(commit for commit, _flags in rows),
            tracked_change_ids=frozenset(state.pr_identities),
            trunk=trunk,
        )
    )
