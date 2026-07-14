from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import jj_stack.ui as ui
from jj_stack.commands.list_ import (
    OrphanRow,
    _emit_orphan_hint,
    _prepare_repo_inspection_context,
)
from jj_stack.config import RepoConfig
from jj_stack.github.resolution import GithubTarget
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.discovery import discover_connected_tracked_stacks, discover_tracked_stacks


def test_orphan_hint_is_emitted_once_for_all_rows(monkeypatch) -> None:
    row = OrphanRow(
        bookmark="review/orphan-aaaaaaaa",
        change_id="a" * 32,
        pull_request={"number": 1},
        review="orphan",
        state="orphan",
        subject="orphan",
    )
    notes: list[ui.Message] = []
    monkeypatch.setattr("jj_stack.commands.list_.console.note", notes.append)

    _emit_orphan_hint((row, row))

    assert len(notes) == 1
    assert "unstack --cleanup --pull-request orphans" in ui.plain_text(notes[0])


def _revision(
    change_id: str,
    commit_id: str,
    *,
    parent: str,
    subject: str,
) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=commit_id,
        current_working_copy=False,
        description=subject,
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=(parent,),
    )


def test_discover_stacks_extends_only_tracked_heads_for_fully_tracked_linear_stack() -> None:
    root = _revision("a" * 32, "commit-a", parent="main", subject="feature 1")
    middle = _revision("b" * 32, "commit-b", parent="commit-a", subject="feature 2")
    head = _revision("c" * 32, "commit-c", parent="commit-b", subject="feature 3")
    tracked_revisions = {
        root.change_id: (root,),
        middle.change_id: (middle,),
        head.change_id: (head,),
    }
    queried_descendants: list[tuple[str, ...]] = []
    queried_base_parents: list[tuple[str, ...]] = []
    queried_trunk_ancestors: list[tuple[str, ...]] = []
    base_parent = root.model_copy(update={"commit_id": "main", "change_id": "m" * 32})

    jj_client = cast(
        Any,
        SimpleNamespace(
            query_revisions_by_change_ids=lambda change_ids: {
                change_id: tracked_revisions[change_id] for change_id in change_ids
            },
            query_descendant_revisions=lambda commit_ids: (
                queried_descendants.append(tuple(commit_ids)) or (head, middle, root)
            ),
            query_revisions_by_commit_ids=lambda commit_ids: (
                queried_base_parents.append(tuple(commit_ids)) or (base_parent,)
            ),
            query_trunk_ancestor_commit_ids=lambda commit_ids: (
                queried_trunk_ancestors.append(tuple(commit_ids)) or {"main"}
            ),
            resolve_revision=lambda revset: base_parent,
        ),
    )
    state = ReviewState(
        changes={
            revision.change_id: CachedChange(last_submitted_commit_id=revision.commit_id)
            for revision in (root, middle, head)
        }
    )

    discovered = discover_tracked_stacks(jj_client=jj_client, state=state)

    assert tuple(stack.head.commit_id for stack in discovered.stacks) == (head.commit_id,)
    assert queried_descendants == [(root.commit_id, middle.commit_id, head.commit_id)]
    assert queried_base_parents == [("main",)]
    assert queried_trunk_ancestors == [("main",)]


def test_connected_stacks_skip_descendant_walk_when_other_tracking_is_unrelated() -> None:
    trunk = _revision("m" * 32, "main", parent="root", subject="main")
    selected = _revision("a" * 32, "commit-a", parent="main", subject="feature A")
    unrelated = _revision("b" * 32, "commit-b", parent="main", subject="feature B")
    selected_stack = LocalStack(
        base_parent=trunk,
        head=selected,
        revisions=(selected,),
        selected_revset=selected.change_id,
        trunk=trunk,
    )
    queried_matches: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def query_connected(change_ids, ancestor_commit_ids):
        queried_matches.append((tuple(change_ids), tuple(ancestor_commit_ids)))
        return ()

    jj_client = cast(
        Any,
        SimpleNamespace(
            query_revisions_by_change_ids_descending_from=query_connected,
            query_descendant_revisions=lambda _commit_ids: (_ for _ in ()).throw(
                AssertionError("unrelated tracking should not walk descendants")
            ),
        ),
    )
    state = ReviewState(
        changes={
            selected.change_id: CachedChange(last_submitted_commit_id=selected.commit_id),
            unrelated.change_id: CachedChange(last_submitted_commit_id=unrelated.commit_id),
        }
    )

    discovered = discover_connected_tracked_stacks(
        jj_client=jj_client,
        selected_stacks=(selected_stack,),
        state=state,
    )

    assert discovered == ()
    assert queried_matches == [((unrelated.change_id,), (selected.commit_id,))]


def test_connected_stacks_warn_for_tracked_change_built_on_selected_stack() -> None:
    trunk = _revision("m" * 32, "main", parent="root", subject="main")
    selected = _revision("a" * 32, "commit-a", parent="main", subject="feature A")
    connected = _revision("b" * 32, "commit-b", parent="commit-a", subject="feature B")
    selected_stack = LocalStack(
        base_parent=trunk,
        head=selected,
        revisions=(selected,),
        selected_revset=selected.change_id,
        trunk=trunk,
    )
    queried_descendants: list[tuple[str, ...]] = []

    def query_descendants(commit_ids):
        queried_descendants.append(tuple(commit_ids))
        return (connected,)

    jj_client = cast(
        Any,
        SimpleNamespace(
            query_revisions_by_change_ids_descending_from=lambda _change_ids,
            _ancestor_commit_ids: (connected,),
            query_descendant_revisions=query_descendants,
            query_revisions_by_commit_ids=lambda _commit_ids: (),
            query_trunk_ancestor_commit_ids=lambda commit_ids: set(commit_ids),
        ),
    )
    state = ReviewState(
        changes={
            selected.change_id: CachedChange(last_submitted_commit_id=selected.commit_id),
            connected.change_id: CachedChange(last_submitted_commit_id=connected.commit_id),
        }
    )

    discovered = discover_connected_tracked_stacks(
        jj_client=jj_client,
        selected_stacks=(selected_stack,),
        state=state,
    )

    assert tuple(stack.head.change_id for stack in discovered) == (connected.change_id,)
    assert queried_descendants == [(connected.commit_id,)]


def test_repo_inspection_limits_bookmark_listing_to_tracked_bookmarks() -> None:
    trunk = _revision("m" * 32, "main", parent="root", subject="main")
    tracked = _revision("a" * 32, "commit-a", parent="main", subject="feature 1")
    untracked = _revision("b" * 32, "commit-b", parent="commit-a", subject="feature 2")
    stack = LocalStack(
        base_parent=trunk,
        head=untracked,
        revisions=(tracked, untracked),
        selected_revset=untracked.change_id,
        trunk=trunk,
    )
    state = ReviewState(
        changes={
            tracked.change_id: CachedChange(
                bookmark="review/feature-1-abcdef01",
                pr_number=1,
                pr_state="open",
            ),
        }
    )
    bookmark_calls: list[tuple[str, ...] | None] = []
    jj_client = cast(
        Any,
        SimpleNamespace(
            list_git_remotes=lambda: (
                GitRemote(name="origin", url="https://github.com/octo-org/repo.git"),
            ),
            list_bookmark_states=lambda bookmarks=None: (
                bookmark_calls.append(bookmarks) or {}
            ),
        ),
    )
    context = cast(Any, SimpleNamespace(config=RepoConfig(), jj_client=jj_client))

    inspection = _prepare_repo_inspection_context(
        context=context,
        discovered=(stack,),
        state=state,
    )

    assert isinstance(inspection.github_target, GithubTarget)
    assert bookmark_calls == [("review/feature-1-abcdef01",)]
