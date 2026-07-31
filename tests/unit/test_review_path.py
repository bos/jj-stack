from __future__ import annotations

import pytest

from jj_stack.errors import AmbiguousSelectionError, CliError
from jj_stack.models.stack import LocalRevision
from jj_stack.review.path import (
    RepositoryPathObservation,
    SelectedPathObservation,
    project_repository_paths,
    project_selected_path,
)


def test_selected_path_is_order_independent_and_tracking_only_annotates() -> None:
    trunk = _revision("trunk", "trunk-change", parents=("root",), immutable=True)
    bottom = _revision("bottom", "bottom-change", parents=("trunk",))
    head = _revision("head", "head-change", parents=("bottom",))
    unrelated = _revision("other", "other-change", parents=("bottom",))

    first = project_selected_path(
        _observation(
            head=head,
            revisions=(unrelated, head, trunk, bottom),
            tracked_change_ids=frozenset({"head-change"}),
            trunk=trunk,
        )
    )
    reordered = project_selected_path(
        _observation(
            head=head,
            revisions=(bottom, trunk, unrelated, head),
            tracked_change_ids=frozenset({"bottom-change", "other-change"}),
            trunk=trunk,
        )
    )

    assert [revision.commit_id for revision in first.stack.revisions] == ["bottom", "head"]
    assert [revision.commit_id for revision in reordered.stack.revisions] == ["bottom", "head"]
    assert first.tracked_change_ids == frozenset({"head-change"})
    assert reordered.tracked_change_ids == frozenset({"bottom-change"})


def test_selected_path_uses_mutable_copy_beside_fetched_rebase_result() -> None:
    trunk = _revision("new-trunk", "trunk-change", parents=("landed",), immutable=True)
    old_trunk = _revision("old-trunk", "old-trunk-change", parents=("root",), immutable=True)
    local = _revision(
        "local",
        "review-change",
        parents=("old-trunk",),
        divergent=True,
    )
    landed = _revision(
        "landed",
        "review-change",
        parents=("old-trunk",),
        divergent=True,
        immutable=True,
    )

    selected = project_selected_path(
        _observation(
            head=local,
            revisions=(trunk, landed, local, old_trunk),
            selector_revisions=(landed, local),
            select_mutable_copy=True,
            trunk=trunk,
            fetched_trunk_commit_ids=frozenset({"old-trunk", "landed", "new-trunk"}),
        )
    )

    assert selected.stack.head.commit_id == "local"
    assert [revision.commit_id for revision in selected.stack.revisions] == ["local"]
    assert selected.stack.base_parent.commit_id == "old-trunk"


def test_selected_path_stops_when_two_mutable_copies_match() -> None:
    trunk = _revision("trunk", "trunk-change", parents=("root",), immutable=True)
    first = _revision("copy-a", "review-change", parents=("trunk",), divergent=True)
    second = _revision("copy-b", "review-change", parents=("trunk",), divergent=True)

    with pytest.raises(AmbiguousSelectionError, match="more than one mutable local copy"):
        project_selected_path(
            _observation(
                head=first,
                revisions=(trunk, first, second),
                selector_revisions=(second, first),
                select_mutable_copy=True,
                trunk=trunk,
            )
        )


def test_only_explicit_revision_selection_can_project_a_sole_trunk_copy() -> None:
    trunk_copy = _revision(
        "landed",
        "review-change",
        parents=("old-trunk",),
        immutable=True,
    )
    explicit = project_selected_path(
        _observation(
            head=trunk_copy,
            revisions=(trunk_copy,),
            trunk=trunk_copy,
        )
    )

    assert explicit.stack.revisions == ()
    with pytest.raises(CliError, match="no mutable local copy"):
        project_selected_path(
            _observation(
                head=trunk_copy,
                revisions=(trunk_copy,),
                selector_revisions=(trunk_copy,),
                select_mutable_copy=True,
                trunk=trunk_copy,
            )
        )


def test_selected_overlap_follows_only_the_explicit_head_parent_path() -> None:
    trunk = _revision("trunk", "trunk-change", parents=("root",), immutable=True)
    shared = _revision("shared", "shared-change", parents=("trunk",))
    left = _revision("left", "left-change", parents=("shared",))
    right = _revision("right", "right-change", parents=("shared",))

    selected = project_selected_path(
        _observation(
            head=right,
            revisions=(left, trunk, right, shared),
            trunk=trunk,
        )
    )

    assert [revision.commit_id for revision in selected.stack.revisions] == ["shared", "right"]


def test_repository_paths_inventory_an_ordinary_shared_prefix() -> None:
    trunk = _revision("trunk", "trunk-change", parents=("root",), immutable=True)
    shared = _revision("shared", "shared-change", parents=("trunk",))
    left = _revision("left", "left-change", parents=("shared",))
    right = _revision("right", "right-change", parents=("shared",))

    projected = project_repository_paths(
        RepositoryPathObservation(
            candidate_commit_ids=frozenset({"shared", "left", "right"}),
            current_review_commit_id=None,
            fetched_trunk_commit_ids=frozenset({"trunk"}),
            revisions=(right, trunk, shared, left),
            tracked_change_ids=frozenset({"left-change", "right-change"}),
            trunk=trunk,
        )
    )

    assert [
        [revision.commit_id for revision in path.stack.revisions] for path in projected.paths
    ] == [["shared", "left"], ["shared", "right"]]


def test_repository_and_selected_projection_agree_for_one_ordinary_path() -> None:
    trunk = _revision("trunk", "trunk-change", parents=("root",), immutable=True)
    shared = _revision("shared", "shared-change", parents=("trunk",))
    left = _revision("left", "left-change", parents=("shared",))
    right = _revision("right", "right-change", parents=("shared",))
    revisions = (left, trunk, right, shared)

    selected = project_selected_path(_observation(head=right, revisions=revisions, trunk=trunk))
    repository = project_repository_paths(
        RepositoryPathObservation(
            candidate_commit_ids=frozenset({"shared", "left", "right"}),
            current_review_commit_id=None,
            fetched_trunk_commit_ids=frozenset({"trunk"}),
            revisions=revisions,
            tracked_change_ids=frozenset(),
            trunk=trunk,
        )
    )
    repository_right = next(
        path for path in repository.paths if path.stack.head.commit_id == "right"
    )

    assert repository_right.stack.revisions == selected.stack.revisions
    assert repository_right.stack.base_parent == selected.stack.base_parent


def _observation(
    *,
    head: LocalRevision,
    revisions: tuple[LocalRevision, ...],
    trunk: LocalRevision,
    fetched_trunk_commit_ids: frozenset[str] | None = None,
    select_mutable_copy: bool = False,
    selector_revisions: tuple[LocalRevision, ...] | None = None,
    tracked_change_ids: frozenset[str] = frozenset(),
) -> SelectedPathObservation:
    return SelectedPathObservation(
        current_working_copy_commit_id=None,
        fetched_trunk_commit_ids=fetched_trunk_commit_ids or frozenset({trunk.commit_id}),
        revisions=revisions,
        selected_revset=head.change_id,
        selector_revisions=selector_revisions or (head,),
        select_mutable_copy=select_mutable_copy,
        tracked_change_ids=tracked_change_ids,
        trunk=trunk,
    )


def _revision(
    commit_id: str,
    change_id: str,
    *,
    parents: tuple[str, ...],
    divergent: bool = False,
    immutable: bool = False,
) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=commit_id,
        current_working_copy=False,
        description=f"{change_id} subject",
        divergent=divergent,
        empty=False,
        hidden=False,
        immutable=immutable,
        parents=parents,
    )
