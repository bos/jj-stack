from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

import jj_stack.ui as ui
from jj_stack.commands.list_ import (
    OrphanRow,
    _emit_orphan_hint,
    _prepare_repo_inspection_context,
)
from jj_stack.config import RepoConfig
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubTarget
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.discovery import (
    discover_connected_tracked_stacks,
    discover_tracked_stacks,
    validate_review_stack_ownership,
)


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
    working_copy: bool = False,
) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=commit_id,
        current_working_copy=working_copy,
        description=subject,
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=(parent,),
    )


def _identity(*, bookmark: str = "review/feature-abcdefgh", pr_number: int = 1) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="repo",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref=bookmark,
        bookmark_ownership="managed",
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
        review_identities={revision.change_id: _identity() for revision in (root, middle, head)},
        submitted_baselines={
            revision.change_id: SubmittedBaseline(commit_id=revision.commit_id)
            for revision in (root, middle, head)
        },
    )

    discovered = discover_tracked_stacks(jj_client=jj_client, state=state)

    assert tuple(stack.head.commit_id for stack in discovered.stacks) == (head.commit_id,)
    assert queried_descendants == [(root.commit_id, middle.commit_id, head.commit_id)]
    assert queried_base_parents == [("main",)]
    assert queried_trunk_ancestors == [("main",)]


def test_review_stack_ownership_counts_active_identity_on_maximal_paths() -> None:
    trunk = _revision("m" * 32, "main", parent="root", subject="main")
    shared = _revision("a" * 32, "commit-a", parent="main", subject="shared")
    first = _revision("b" * 32, "commit-b", parent="commit-a", subject="first")
    second = _revision("c" * 32, "commit-c", parent="commit-a", subject="second")
    jj_client = cast(
        Any,
        SimpleNamespace(
            query_descendant_revisions=lambda _commit_ids: (shared, first, second),
            query_revisions_by_commit_ids=lambda _commit_ids: (trunk,),
            query_trunk_ancestor_commit_ids=lambda commit_ids: set(commit_ids),
            resolve_revision=lambda _revset: trunk,
        ),
    )
    identity = _identity()

    with pytest.raises(CliError, match="more than one local stack") as caught:
        validate_review_stack_ownership(
            jj_client=jj_client,
            selected_revisions=(shared, first),
            state=ReviewState(review_identities={shared.change_id: identity}),
        )
    assert "jj log" in str(caught.value)
    assert "jj rebase" in str(caught.value)

    validate_review_stack_ownership(
        jj_client=jj_client,
        selected_revisions=(shared, first),
        state=ReviewState(
            review_identities={
                shared.change_id: identity.model_copy(update={"link_state": "unlinked"})
            }
        ),
    )


def test_review_stack_ownership_accepts_a_selected_prefix_of_one_maximal_path() -> None:
    trunk = _revision("m" * 32, "main", parent="root", subject="main")
    root = _revision("a" * 32, "commit-a", parent="main", subject="root")
    middle = _revision("b" * 32, "commit-b", parent="commit-a", subject="middle")
    head = _revision("c" * 32, "commit-c", parent="commit-b", subject="head")
    jj_client = cast(
        Any,
        SimpleNamespace(
            query_descendant_revisions=lambda _commit_ids: (root, middle, head),
            query_revisions_by_commit_ids=lambda _commit_ids: (trunk,),
            query_trunk_ancestor_commit_ids=lambda commit_ids: set(commit_ids),
            resolve_revision=lambda _revset: trunk,
        ),
    )

    validate_review_stack_ownership(
        jj_client=jj_client,
        selected_revisions=(root, middle),
        state=ReviewState(review_identities={root.change_id: _identity()}),
        prospective_change_ids=frozenset((middle.change_id,)),
    )


def test_review_stack_ownership_ignores_an_unrelated_invalid_component() -> None:
    trunk = _revision("m" * 32, "main", parent="root", subject="main")
    selected = _revision("a" * 32, "selected", parent="main", subject="selected")
    selected_head = _revision("b" * 32, "selected-head", parent="selected", subject="head")
    unrelated = _revision("c" * 32, "unrelated", parent="main", subject="unrelated")
    queried_descendants: list[tuple[str, ...]] = []
    jj_client = cast(
        Any,
        SimpleNamespace(
            query_descendant_revisions=lambda commit_ids: (
                queried_descendants.append(tuple(commit_ids)) or (selected, selected_head)
            ),
            query_revisions_by_commit_ids=lambda _commit_ids: (trunk,),
            query_trunk_ancestor_commit_ids=lambda commit_ids: set(commit_ids),
            resolve_revision=lambda _revset: trunk,
        ),
    )

    validate_review_stack_ownership(
        jj_client=jj_client,
        selected_revisions=(selected, selected_head),
        state=ReviewState(
            review_identities={
                selected.change_id: _identity(),
                unrelated.change_id: _identity(pr_number=2),
            }
        ),
    )

    assert queried_descendants == [(selected.commit_id, selected_head.commit_id)]


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
        review_identities={
            selected.change_id: _identity(),
            unrelated.change_id: _identity(),
        },
        submitted_baselines={
            selected.change_id: SubmittedBaseline(commit_id=selected.commit_id),
            unrelated.change_id: SubmittedBaseline(commit_id=unrelated.commit_id),
        },
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
            query_revisions_by_change_ids_descending_from=(
                lambda _change_ids, _ancestor_commit_ids: (connected,)
            ),
            query_descendant_revisions=query_descendants,
            query_revisions_by_commit_ids=lambda _commit_ids: (),
            query_trunk_ancestor_commit_ids=lambda commit_ids: set(commit_ids),
        ),
    )
    state = ReviewState(
        review_identities={
            selected.change_id: _identity(),
            connected.change_id: _identity(),
        },
        submitted_baselines={
            selected.change_id: SubmittedBaseline(commit_id=selected.commit_id),
            connected.change_id: SubmittedBaseline(commit_id=connected.commit_id),
        },
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
        review_identities={
            tracked.change_id: _identity(bookmark="review/feature-1-abcdef01"),
        }
    )
    bookmark_calls: list[tuple[str, ...] | None] = []
    jj_client = cast(
        Any,
        SimpleNamespace(
            list_git_remotes=lambda: (
                GitRemote(
                    name="origin",
                    fetch_url="https://github.com/octo-org/repo.git",
                    push_url="https://github.com/octo-org/repo.git",
                ),
            ),
            list_bookmark_states=lambda bookmarks=None: bookmark_calls.append(bookmarks) or {},
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
