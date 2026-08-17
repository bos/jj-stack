from __future__ import annotations

import pytest

from jj_stack.errors import AmbiguousSelectionError, CliError
from jj_stack.models.stack import LocalCommit
from jj_stack.stack.path import (
    RepoPathObservation,
    SelectedPathObservation,
    project_repo_paths,
    project_selected_path,
)


def test_selected_path_is_order_independent() -> None:
    trunk = _change("trunk", "trunk-change", parents=("root",), immutable=True)
    bottom = _change("bottom", "bottom-change", parents=("trunk",))
    head = _change("head", "head-change", parents=("bottom",))
    unrelated = _change("other", "other-change", parents=("bottom",))

    first = project_selected_path(
        _observation(
            head=head,
            commits=(unrelated, head, trunk, bottom),
            trunk=trunk,
        )
    )
    reordered = project_selected_path(
        _observation(
            head=head,
            commits=(bottom, trunk, unrelated, head),
            trunk=trunk,
        )
    )

    assert [change.commit_id for change in first.stack.changes] == ["bottom", "head"]
    assert [change.commit_id for change in reordered.stack.changes] == ["bottom", "head"]


def test_selected_path_uses_mutable_copy_beside_fetched_rebase_result() -> None:
    trunk = _change("new-trunk", "trunk-change", parents=("landed",), immutable=True)
    old_trunk = _change("old-trunk", "old-trunk-change", parents=("root",), immutable=True)
    local = _change(
        "local",
        "pr-change",
        parents=("old-trunk",),
        divergent=True,
    )
    landed = _change(
        "landed",
        "pr-change",
        parents=("old-trunk",),
        divergent=True,
        immutable=True,
    )

    selected = project_selected_path(
        _observation(
            head=local,
            commits=(trunk, landed, local, old_trunk),
            selector_commits=(landed, local),
            select_mutable_copy=True,
            trunk=trunk,
            fetched_trunk_commit_ids=frozenset({"old-trunk", "landed", "new-trunk"}),
        )
    )

    assert selected.stack.head.commit_id == "local"
    assert [change.commit_id for change in selected.stack.changes] == ["local"]
    assert selected.stack.base_parent.commit_id == "old-trunk"


def test_selected_path_stops_when_two_mutable_copies_match() -> None:
    trunk = _change("trunk", "trunk-change", parents=("root",), immutable=True)
    first = _change("copy-a", "pr-change", parents=("trunk",), divergent=True)
    second = _change("copy-b", "pr-change", parents=("trunk",), divergent=True)

    with pytest.raises(AmbiguousSelectionError, match="more than one mutable local copy"):
        project_selected_path(
            _observation(
                head=first,
                commits=(trunk, first, second),
                selector_commits=(second, first),
                select_mutable_copy=True,
                trunk=trunk,
            )
        )


def test_only_explicit_change_selection_can_project_a_sole_trunk_copy() -> None:
    trunk_copy = _change(
        "landed",
        "pr-change",
        parents=("old-trunk",),
        immutable=True,
    )
    explicit = project_selected_path(
        _observation(
            head=trunk_copy,
            commits=(trunk_copy,),
            trunk=trunk_copy,
        )
    )

    assert explicit.stack.changes == ()
    with pytest.raises(CliError, match="already on trunk"):
        project_selected_path(
            _observation(
                head=trunk_copy,
                commits=(trunk_copy,),
                selector_commits=(trunk_copy,),
                select_mutable_copy=True,
                trunk=trunk_copy,
            )
        )


def test_selected_overlap_follows_only_the_explicit_head_parent_path() -> None:
    trunk = _change("trunk", "trunk-change", parents=("root",), immutable=True)
    shared = _change("shared", "shared-change", parents=("trunk",))
    left = _change("left", "left-change", parents=("shared",))
    right = _change("right", "right-change", parents=("shared",))

    selected = project_selected_path(
        _observation(
            head=right,
            commits=(left, trunk, right, shared),
            trunk=trunk,
        )
    )

    assert [change.commit_id for change in selected.stack.changes] == ["shared", "right"]


def test_selected_path_fails_closed_when_its_parent_boundary_was_not_observed() -> None:
    trunk = _change("trunk", "trunk-change", parents=("root",), immutable=True)
    head = _change("head", "head-change", parents=("missing",))

    with pytest.raises(CliError, match="unobserved parent missing"):
        project_selected_path(_observation(head=head, commits=(head, trunk), trunk=trunk))


def test_repo_paths_inventory_an_ordinary_shared_prefix() -> None:
    trunk = _change("trunk", "trunk-change", parents=("root",), immutable=True)
    shared = _change("shared", "shared-change", parents=("trunk",))
    left = _change("left", "left-change", parents=("shared",))
    right = _change("right", "right-change", parents=("shared",))

    projected = project_repo_paths(
        RepoPathObservation(
            candidate_commit_ids=frozenset({"shared", "left", "right"}),
            current_tracked_commit_id=None,
            fetched_trunk_commit_ids=frozenset({"trunk"}),
            commits=(right, trunk, shared, left),
            tracked_change_ids=frozenset({"left-change", "right-change"}),
            trunk=trunk,
        )
    )

    assert [[change.commit_id for change in path.stack.changes] for path in projected.paths] == [
        ["shared", "left"],
        ["shared", "right"],
    ]


def test_repo_and_selected_projection_agree_for_one_ordinary_path() -> None:
    trunk = _change("trunk", "trunk-change", parents=("root",), immutable=True)
    shared = _change("shared", "shared-change", parents=("trunk",))
    left = _change("left", "left-change", parents=("shared",))
    right = _change("right", "right-change", parents=("shared",))
    commits = (left, trunk, right, shared)

    selected = project_selected_path(_observation(head=right, commits=commits, trunk=trunk))
    repo = project_repo_paths(
        RepoPathObservation(
            candidate_commit_ids=frozenset({"shared", "left", "right"}),
            current_tracked_commit_id=None,
            fetched_trunk_commit_ids=frozenset({"trunk"}),
            commits=commits,
            tracked_change_ids=frozenset(),
            trunk=trunk,
        )
    )
    repo_right = next(path for path in repo.paths if path.stack.head.commit_id == "right")

    assert repo_right.stack.changes == selected.stack.changes
    assert repo_right.stack.base_parent == selected.stack.base_parent


def _observation(
    *,
    head: LocalCommit,
    commits: tuple[LocalCommit, ...],
    trunk: LocalCommit,
    fetched_trunk_commit_ids: frozenset[str] | None = None,
    select_mutable_copy: bool = False,
    selector_commits: tuple[LocalCommit, ...] | None = None,
) -> SelectedPathObservation:
    return SelectedPathObservation(
        candidate_commit_ids=frozenset(
            commit.commit_id for commit in commits if commit.commit_id != trunk.commit_id
        ),
        current_working_copy_commit_id=None,
        fetched_trunk_commit_ids=fetched_trunk_commit_ids or frozenset({trunk.commit_id}),
        commits=commits,
        selected_revset=head.change_id,
        selector_commits=selector_commits or (head,),
        select_mutable_copy=select_mutable_copy,
        trunk=trunk,
    )


def _change(
    commit_id: str,
    change_id: str,
    *,
    parents: tuple[str, ...],
    divergent: bool = False,
    immutable: bool = False,
) -> LocalCommit:
    return LocalCommit(
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
