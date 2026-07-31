"""Observe ordinary repository paths for the pure path projection."""

from __future__ import annotations

import json
from collections.abc import Sequence

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.review.path import (
    RepositoryPathObservation,
    RepositoryReviewPaths,
    project_repository_paths,
)


def observe_repository_paths(
    *,
    jj_client: JjClient,
    tracked_change_ids: Sequence[str],
    descendant_of: Sequence[str] = (),
    include_current_working_copy: bool = False,
) -> RepositoryReviewPaths:
    """Batch the visible facts for ordinary maximal paths.

    With no anchors this observes the repository inventory. Exact commit anchors
    narrow the observation to paths descending from those revisions.
    """

    trunk_path = "first_ancestors(trunk())"
    visible_scope = "visible()"
    if descendant_of:
        anchors = " | ".join(json.dumps(commit_id) for commit_id in descendant_of)
        visible_scope = f"(visible() & ({anchors})::)"
    candidates = f"(({visible_scope}) ~ {trunk_path} ~ working_copies())"
    if include_current_working_copy:
        if not descendant_of:
            raise ValueError("Working-copy dependency observation requires an exact ancestor.")
        candidates = f"({candidates} | (@ & {visible_scope}))"
    rows = jj_client.query_revisions_with_membership(
        f"trunk() | ({candidates}) | parents({candidates}) | @",
        membership_revsets=("trunk()", candidates, trunk_path),
    )
    trunks = tuple(revision for revision, flags in rows if flags[0])
    if len(trunks) != 1:
        raise CliError(t"Could not resolve {ui.revset('trunk()')} to one revision.")
    trunk = trunks[0]
    if not trunk.parents:
        raise UnsupportedStackError(
            t"No trunk bookmark is configured for this repo.",
            hint=t"Create a trunk bookmark such as {ui.bookmark('main')}, then retry.",
            reason="trunk_resolved_to_root",
        )
    current_working_copy = next(
        (revision for revision, _flags in rows if revision.current_working_copy),
        None,
    )
    current_commit_id = (
        current_working_copy.parents[0]
        if current_working_copy is not None and current_working_copy.parents
        else None
    )
    return project_repository_paths(
        RepositoryPathObservation(
            candidate_commit_ids=frozenset(
                revision.commit_id for revision, flags in rows if flags[1]
            ),
            current_commit_id=current_commit_id,
            fetched_trunk_commit_ids=frozenset(
                revision.commit_id for revision, flags in rows if flags[2]
            ),
            revisions=tuple(revision for revision, _flags in rows),
            tracked_change_ids=frozenset(tracked_change_ids),
            trunk=trunk,
        )
    )
