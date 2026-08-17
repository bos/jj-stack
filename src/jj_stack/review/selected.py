"""Observe and project one ordinary selected review path."""

from __future__ import annotations

import json
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.jj.client import (
    JjClient,
    JjCommandError,
    UnsupportedStackError,
    divergent_change_id_from_error,
)
from jj_stack.models.review_state import ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import prepare_visible_review_snapshots
from jj_stack.review.path import (
    SelectedPathObservation,
    SelectedReviewPath,
    project_selected_path,
)


@dataclass(frozen=True, slots=True)
class _ObservedPathRow:
    """One revision with named membership in the path observation revsets."""

    revision: LocalRevision
    is_trunk: bool
    is_selector: bool
    is_linked_selector: bool
    is_candidate: bool
    is_path: bool
    is_trunk_path: bool


def select_review_path(
    *,
    inspection_mode: bool = False,
    jj_client: JjClient,
    state: ReviewState,
    revset: str | None = None,
) -> SelectedReviewPath:
    """Collect the bounded facts for one selector and project its parent path."""

    prepare_visible_review_snapshots(jj_client=jj_client, state=state)

    if revset is None:
        selector = "@ | @-"
        selected_revset = "@"
        select_mutable_copy = False
    elif _is_full_change_id(revset):
        selector = _change_id_revset(revset)
        selected_revset = revset
        select_mutable_copy = True
    else:
        selector = revset
        selected_revset = revset
        select_mutable_copy = False

    try:
        rows = _observe_path_rows(
            jj_client=jj_client,
            selector=selector,
            selected_revset=revset,
        )
    except JjCommandError as error:
        if revset is None or divergent_change_id_from_error(error) != revset:
            raise
        selector = _change_id_revset(revset)
        select_mutable_copy = True
        rows = _observe_path_rows(
            jj_client=jj_client,
            selector=selector,
            selected_revset=revset,
        )
    path = _project_rows(
        rows=rows,
        selected_revset=selected_revset,
        select_mutable_copy=select_mutable_copy,
        selector_revisions=tuple(row.revision for row in rows if row.is_selector),
        state=state,
        use_default=revset is None,
        inspection_mode=inspection_mode,
    )
    if revset is None and path.stack.head.current_working_copy:
        return _replace_selected_revset(path, "@")
    if revset is None:
        return _replace_selected_revset(path, "@-")
    return path


def select_review_path_containing_change(
    *,
    inspection_mode: bool = False,
    change_id: str,
    jj_client: JjClient,
    state: ReviewState,
) -> SelectedReviewPath:
    """Project the unique ordinary path whose head descends from one tracked change."""

    prepare_visible_review_snapshots(jj_client=jj_client, state=state)
    linked_selector = _change_id_revset(change_id)
    trunk_path = "first_ancestors(trunk())"
    nonempty_descendants = f"((({linked_selector}) ~ {trunk_path}):: ~ {trunk_path}) ~ empty()"
    selected_empty_change = f"({linked_selector}) & empty()"
    containing_heads = f"heads(({nonempty_descendants}) | ({selected_empty_change}))"
    rows = _observe_path_rows(
        jj_client=jj_client,
        linked_selector=linked_selector,
        selector=containing_heads,
        selected_revset=change_id,
    )
    selected_change_path = _project_rows(
        rows=rows,
        selected_revset=change_id,
        select_mutable_copy=True,
        selector_revisions=tuple(row.revision for row in rows if row.is_linked_selector),
        state=state,
        use_default=False,
        inspection_mode=inspection_mode,
    )
    heads = tuple(row.revision for row in rows if row.is_selector)
    containing_heads = _heads_containing_commit(
        commit_id=selected_change_path.stack.head.commit_id,
        heads=heads,
        revisions=tuple(row.revision for row in rows),
    )
    selected_revset = containing_heads[0].change_id if len(containing_heads) == 1 else change_id
    return _project_rows(
        candidate_commit_ids=frozenset(head.commit_id for head in containing_heads),
        rows=rows,
        selected_revset=selected_revset,
        select_mutable_copy=False,
        selector_revisions=tuple(row.revision for row in rows if row.is_selector),
        state=state,
        use_default=False,
        inspection_mode=inspection_mode,
    )


def require_reviewable_revisions(revisions: tuple[LocalRevision, ...]) -> None:
    """Require the ordinary mutable revisions accepted for publishing or relinking."""

    for revision in revisions:
        if revision.immutable:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "immutable commits are not reviewable.",
                reason="immutable_commit",
            )
        if revision.divergent:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "divergent changes are not supported.",
                reason="divergent_change",
            )


def _observe_path_rows(
    *,
    jj_client: JjClient,
    linked_selector: str | None = None,
    selector: str,
    selected_revset: str | None,
) -> tuple[_ObservedPathRow, ...]:
    trunk_path = "first_ancestors(trunk())"
    off_trunk = f"({selector}) ~ {trunk_path}"
    ancestors = f"first_ancestors({off_trunk})"
    trunk_boundaries = f"parents(({ancestors}) ~ {trunk_path}) & {trunk_path}"
    candidate_neighborhood = f"(visible() & (({selector}) | children({selector})))"
    candidate_revisions = (
        f"((({candidate_neighborhood}) ~ {trunk_path} ~ working_copies()) "
        f"| (@ & {candidate_neighborhood}))"
    )
    linked_selector_membership = linked_selector or "none()"
    query = " | ".join(
        (
            "trunk()",
            f"({selector})",
            f"({ancestors}) ~ {trunk_path}",
            trunk_boundaries,
            candidate_revisions,
            *((linked_selector,) if linked_selector is not None else ()),
        )
    )
    raw_rows = jj_client.query_revisions_with_membership(
        query,
        membership_revsets=(
            "trunk()",
            selector,
            linked_selector_membership,
            candidate_revisions,
            ancestors,
            trunk_path,
        ),
        selected_revset=selected_revset,
    )
    return tuple(
        _ObservedPathRow(
            revision=revision,
            is_trunk=is_trunk,
            is_selector=is_selector,
            is_linked_selector=is_linked_selector,
            is_candidate=is_candidate,
            is_path=is_path,
            is_trunk_path=is_trunk_path,
        )
        for revision, (
            is_trunk,
            is_selector,
            is_linked_selector,
            is_candidate,
            is_path,
            is_trunk_path,
        ) in raw_rows
    )


def _project_rows(
    *,
    candidate_commit_ids: frozenset[str] | None = None,
    inspection_mode: bool,
    rows: tuple[_ObservedPathRow, ...],
    selected_revset: str,
    select_mutable_copy: bool,
    selector_revisions: tuple[LocalRevision, ...],
    state: ReviewState,
    use_default: bool,
) -> SelectedReviewPath:
    trunks = tuple(row.revision for row in rows if row.is_trunk)
    if len(trunks) != 1:
        raise CliError(t"Could not resolve {ui.revset('trunk()')} to one revision.")
    trunk = trunks[0]
    if not trunk.parents:
        raise UnsupportedStackError(
            "No trunk bookmark is configured for this repo.",
            hint=t"Create a trunk bookmark such as {ui.bookmark('main')}, then retry.",
            reason="trunk_resolved_to_root",
        )

    candidates = tuple(
        revision
        for revision in selector_revisions
        if candidate_commit_ids is None or revision.commit_id in candidate_commit_ids
    )
    if not candidates:
        raise CliError(
            t"Revset {ui.revset(selected_revset)} did not resolve to a visible revision."
        )
    current_working_copy_commit_id = (
        next(
            (revision.commit_id for revision in candidates if revision.current_working_copy),
            None,
        )
        if use_default
        else None
    )
    path_revisions = tuple(row.revision for row in rows if row.is_path)
    if not inspection_mode and any(len(revision.parents) > 1 for revision in path_revisions):
        raise UnsupportedStackError(
            "Unsupported stack shape: merge commits are not supported.",
            reason="merge_commit",
        )
    if any(not revision.parents for revision in path_revisions):
        raise UnsupportedStackError(
            t"Unsupported stack shape: selected-parent path reached the root commit before "
            t"{ui.revset('trunk()')}.",
            reason="reached_root_before_trunk",
        )
    path = project_selected_path(
        SelectedPathObservation(
            candidate_commit_ids=frozenset(
                row.revision.commit_id for row in rows if row.is_candidate
            ),
            current_working_copy_commit_id=current_working_copy_commit_id,
            fetched_trunk_commit_ids=frozenset(
                row.revision.commit_id for row in rows if row.is_trunk_path
            ),
            revisions=tuple(row.revision for row in rows),
            selected_revset=selected_revset,
            selector_revisions=candidates,
            select_mutable_copy=select_mutable_copy,
            trunk=trunk,
        )
    )
    _validate_selected_path(path, inspection_mode=inspection_mode)
    return path


def _heads_containing_commit(
    *,
    commit_id: str,
    heads: tuple[LocalRevision, ...],
    revisions: tuple[LocalRevision, ...],
) -> tuple[LocalRevision, ...]:
    revisions_by_commit_id = {revision.commit_id: revision for revision in revisions}
    containing: list[LocalRevision] = []
    for head in heads:
        current = head
        while current.commit_id != commit_id and current.parents:
            parent = revisions_by_commit_id.get(current.parents[0])
            if parent is None:
                break
            current = parent
        if current.commit_id == commit_id:
            containing.append(head)
    return tuple(containing)


def _validate_selected_path(
    path: SelectedReviewPath,
    *,
    inspection_mode: bool,
) -> None:
    if inspection_mode:
        return
    for revision in path.stack.revisions:
        if revision.is_working_copy and revision.empty:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "empty working-copy commits are not reviewable.",
                reason="empty_working_copy",
            )
        if revision.is_working_copy and not revision.description.strip():
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                t"describe it with {ui.cmd('jj describe')} before submitting it for review.",
                reason="undescribed_working_copy",
            )


def _replace_selected_revset(
    path: SelectedReviewPath,
    selected_revset: str,
) -> SelectedReviewPath:
    return SelectedReviewPath(
        is_maximal=path.is_maximal,
        stack=path.stack.model_copy(update={"selected_revset": selected_revset}),
    )


def _change_id_revset(change_id: str) -> str:
    return f"change_id({json.dumps(change_id)})"


def _is_full_change_id(value: str) -> bool:
    return len(value) == 32 and is_change_id_prefix(value)


def is_change_id_prefix(value: str | None) -> bool:
    """Return whether a bare selector has jj change-ID syntax."""

    return (
        value is not None and bool(value) and all("k" <= character <= "z" for character in value)
    )
