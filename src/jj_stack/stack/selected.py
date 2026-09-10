"""Observe and project one ordinary selected stack path."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import UnsupportedStackError
from jj_stack.identifiers import short_change_id
from jj_stack.jj.client import (
    JjClient,
    JjCommandError,
    divergent_change_id_from_error,
    quote_revset_symbol,
)
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.observation import TRUNK_PATH, observe_stack_commits
from jj_stack.stack.path import (
    SelectedPathObservation,
    SelectedStackPath,
    project_selected_path,
)
from jj_stack.stack.trunk import require_usable_trunk


@dataclass(frozen=True, slots=True)
class _ObservedPathRow:
    """One commit with named membership in the path observation revsets."""

    commit: LocalCommit
    is_trunk: bool
    is_selector: bool
    is_linked_selector: bool
    is_candidate: bool
    is_path: bool
    is_trunk_path: bool


def select_stack_path(
    *,
    inspection_mode: bool = False,
    jj_client: JjClient,
    state: TrackingState,
    revset: str | None = None,
) -> SelectedStackPath:
    """Read the commits needed to follow a selector back to trunk."""

    if revset is None:
        selector = "@ | @-"
        selected_revset = "@"
        select_mutable_copy = False
    elif len(revset) == 32 and is_change_id_prefix(revset):
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
            state=state,
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
            state=state,
            selector=selector,
            selected_revset=revset,
        )
    path = _project_rows(
        rows=rows,
        selected_revset=selected_revset,
        select_mutable_copy=select_mutable_copy,
        selector_commits=tuple(row.commit for row in rows if row.is_selector),
        use_default=revset is None,
        inspection_mode=inspection_mode,
    )
    if revset is None and path.stack.head.current_working_copy:
        return _replace_selected_revset(path, "@")
    if revset is None:
        return _replace_selected_revset(path, "@-")
    return path


def select_stack_path_containing_change(
    *,
    inspection_mode: bool = False,
    change_id: str,
    jj_client: JjClient,
    state: TrackingState,
) -> SelectedStackPath:
    """Project the unique ordinary path whose head descends from one tracked change."""

    linked_selector = _change_id_revset(change_id)
    nonempty_descendants = f"((({linked_selector}) ~ {TRUNK_PATH}):: ~ {TRUNK_PATH}) ~ empty()"
    selected_empty_change = f"({linked_selector}) & empty()"
    head_revset = f"heads(({nonempty_descendants}) | ({selected_empty_change}))"
    # Bind the derived heads before embedding the selector throughout the path scan. Repeating
    # this expression in every membership predicate makes jj recursively reevaluate it.
    observed_heads = jj_client.query_commits(head_revset)
    bound_heads = (
        " | ".join(f"present({quote_revset_symbol(head.commit_id)})" for head in observed_heads)
        if observed_heads
        else "none()"
    )
    if observed_heads:
        bound_heads = f"visible() & ({bound_heads})"
    rows = _observe_path_rows(
        jj_client=jj_client,
        state=state,
        linked_selector=linked_selector,
        selector=bound_heads,
        selected_revset=change_id,
    )
    selected_change_path = _project_rows(
        rows=rows,
        selected_revset=change_id,
        select_mutable_copy=True,
        selector_commits=tuple(row.commit for row in rows if row.is_linked_selector),
        use_default=False,
        inspection_mode=inspection_mode,
    )
    heads = tuple(row.commit for row in rows if row.is_selector)
    containing_heads = _heads_containing_commit(
        commit_id=selected_change_path.stack.head.commit_id,
        heads=heads,
        commits=tuple(row.commit for row in rows),
    )
    selected_revset = containing_heads[0].change_id if len(containing_heads) == 1 else change_id
    return _project_rows(
        candidate_commit_ids=frozenset(head.commit_id for head in containing_heads),
        rows=rows,
        selected_revset=selected_revset,
        select_mutable_copy=False,
        selector_commits=tuple(row.commit for row in rows if row.is_selector),
        use_default=False,
        inspection_mode=inspection_mode,
    )


def require_submittable_changes(changes: tuple[LocalCommit, ...]) -> None:
    """Require the ordinary mutable changes accepted for publishing or relinking."""

    for change in changes:
        if change.hidden:
            raise UnsupportedStackError.stack_shape(
                change.change_id,
                "hidden changes cannot be submitted.",
                reason="hidden_commit",
            )
        if change.immutable:
            raise UnsupportedStackError.stack_shape(
                change.change_id,
                "immutable changes cannot be submitted.",
                reason="immutable_commit",
            )
        if change.divergent:
            raise UnsupportedStackError.stack_shape(
                change.change_id,
                "divergent changes are not supported.",
                hint=divergence_recovery_hint(
                    change.change_id,
                    retry="retry the jj-stack command",
                ),
                reason="divergent_change",
            )
        if change.empty:
            raise UnsupportedStackError.stack_shape(
                change.change_id,
                t"this change is empty; abandon it with "
                t"{ui.cmd(f'jj abandon {short_change_id(change.change_id)}')} or give it "
                t"content, then retry.",
                reason="empty_change",
            )
        if not change.description.strip():
            raise UnsupportedStackError.stack_shape(
                change.change_id,
                t"describe it with "
                t"{ui.cmd(f'jj describe {short_change_id(change.change_id)}')} and retry.",
                reason="undescribed_change",
            )


def _observe_path_rows(
    *,
    jj_client: JjClient,
    linked_selector: str | None = None,
    state: TrackingState,
    selector: str,
    selected_revset: str | None,
) -> tuple[_ObservedPathRow, ...]:
    off_trunk = f"({selector}) ~ {TRUNK_PATH}"
    ancestors = f"first_ancestors({off_trunk})"
    trunk_boundaries = f"parents(({ancestors}) ~ {TRUNK_PATH}) & {TRUNK_PATH}"
    candidate_neighborhood = f"(visible() & (({selector}) | children({selector})))"
    candidate_commits = f"({candidate_neighborhood} ~ {TRUNK_PATH})"
    linked_selector_membership = linked_selector or "none()"
    query = " | ".join(
        (
            "trunk()",
            f"({selector})",
            f"({ancestors}) ~ {TRUNK_PATH}",
            trunk_boundaries,
            candidate_commits,
            *((linked_selector,) if linked_selector is not None else ()),
        )
    )
    raw_rows = observe_stack_commits(
        jj_client=jj_client,
        state=state,
        revset=query,
        membership_revsets=(
            "trunk()",
            selector,
            linked_selector_membership,
            candidate_commits,
            ancestors,
            TRUNK_PATH,
        ),
        selected_revset=selected_revset,
    ).rows
    return tuple(
        _ObservedPathRow(
            commit=commit,
            is_trunk=is_trunk,
            is_selector=is_selector,
            is_linked_selector=is_linked_selector,
            is_candidate=is_candidate,
            is_path=is_path,
            is_trunk_path=is_trunk_path,
        )
        for commit, (
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
    selector_commits: tuple[LocalCommit, ...],
    use_default: bool,
) -> SelectedStackPath:
    trunks = tuple(row.commit for row in rows if row.is_trunk)
    trunk = require_usable_trunk(trunks)

    candidates = tuple(
        commit
        for commit in selector_commits
        if candidate_commit_ids is None or commit.commit_id in candidate_commit_ids
    )
    if not candidates:
        # A selector that lands on no visible candidate selects no stack, whether the change is
        # hidden or never existed, so it carries the stack-selection exit code.
        raise UnsupportedStackError(
            t"Revset {ui.revset(selected_revset)} did not resolve to a visible commit.",
            hint=t"List the visible changes with {ui.cmd('jj log')}, then select one of them.",
        )
    current_working_copy_commit_id = (
        next(
            (commit.commit_id for commit in candidates if commit.current_working_copy),
            None,
        )
        if use_default
        else None
    )
    path_commits = tuple(row.commit for row in rows if row.is_path)
    # trunk() and its first-parent ancestors are the stack's base rather than stack members, so
    # the merge commit GitHub's default merge method leaves at the tip of the default branch must
    # not fail the selected stack's shape rules.
    if not inspection_mode and any(
        len(row.commit.parents) > 1 for row in rows if row.is_path and not row.is_trunk_path
    ):
        raise UnsupportedStackError(
            "The selected stack includes a change with multiple parents.",
            reason="merge_commit",
        )
    if any(not commit.parents for commit in path_commits):
        raise UnsupportedStackError(
            t"The selected change does not descend from {ui.revset('trunk()')}.",
            reason="reached_root_before_trunk",
        )
    path = project_selected_path(
        SelectedPathObservation(
            candidate_commit_ids=frozenset(
                row.commit.commit_id for row in rows if row.is_candidate
            ),
            current_working_copy_commit_id=current_working_copy_commit_id,
            trunk_first_parent_ids=frozenset(
                row.commit.commit_id for row in rows if row.is_trunk_path
            ),
            commits=tuple(row.commit for row in rows),
            selected_revset=selected_revset,
            selector_commits=candidates,
            select_mutable_copy=select_mutable_copy,
            trunk=trunk,
        )
    )
    _validate_selected_path(path, inspection_mode=inspection_mode)
    return path


def _heads_containing_commit(
    *,
    commit_id: str,
    heads: tuple[LocalCommit, ...],
    commits: tuple[LocalCommit, ...],
) -> tuple[LocalCommit, ...]:
    commits_by_id = {commit.commit_id: commit for commit in commits}
    containing: list[LocalCommit] = []
    for head in heads:
        current = head
        while current.commit_id != commit_id and current.parents:
            parent = commits_by_id.get(current.parents[0])
            if parent is None:
                break
            current = parent
        if current.commit_id == commit_id:
            containing.append(head)
    return tuple(containing)


def _validate_selected_path(
    path: SelectedStackPath,
    *,
    inspection_mode: bool,
) -> None:
    if inspection_mode:
        return
    for change in path.stack.changes:
        if change.is_working_copy and not change.description.strip():
            raise UnsupportedStackError.stack_shape(
                change.change_id,
                t"describe it with "
                t"{ui.cmd(f'jj describe {short_change_id(change.change_id)}')} and retry.",
                reason="undescribed_change",
            )


def _replace_selected_revset(
    path: SelectedStackPath,
    selected_revset: str,
) -> SelectedStackPath:
    return SelectedStackPath(
        is_maximal=path.is_maximal,
        stack=path.stack.model_copy(update={"selected_revset": selected_revset}),
    )


def _change_id_revset(change_id: str) -> str:
    return f"change_id({quote_revset_symbol(change_id)})"


def is_change_id_prefix(value: str | None) -> bool:
    """Return whether a bare selector has jj change-ID syntax."""

    return (
        value is not None and bool(value) and all("k" <= character <= "z" for character in value)
    )
