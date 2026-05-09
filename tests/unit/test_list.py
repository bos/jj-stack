from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

from jj_review import console as console_module
from jj_review.commands.list_ import _state_from_status
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.discovery import discover_connected_tracked_stacks, discover_tracked_stacks
from jj_review.review.status import ReviewStatusRevision


def _render(value: object) -> str:
    stdout = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=StringIO(), color_mode="never"):
        console_module.output(value)
    return stdout.getvalue()


def _open_revision(
    *, is_draft: bool = False, review_decision: str | None = None
) -> ReviewStatusRevision:
    return cast(
        Any,
        SimpleNamespace(
            cached_change=CachedChange(pr_number=7, pr_state="open"),
            commit_id="commit-open",
            has_merged_pull_request=lambda: False,
            link_state="active",
            local_divergent=False,
            pull_request_lookup=SimpleNamespace(
                pull_request=SimpleNamespace(is_draft=is_draft, state="open"),
                review_decision=review_decision,
                review_decision_error=None,
                state="open",
            ),
            remote_state=None,
        ),
    )


def _missing_revision() -> ReviewStatusRevision:
    return cast(
        Any,
        SimpleNamespace(
            cached_change=CachedChange(
                bookmark="review/example",
                pr_number=7,
                pr_state="open",
                pr_url="https://example.test/pr/7",
            ),
            commit_id="commit-missing",
            has_merged_pull_request=lambda: False,
            link_state="active",
            local_divergent=False,
            pull_request_lookup=SimpleNamespace(
                pull_request=None,
                review_decision=None,
                review_decision_error=None,
                state="missing",
            ),
            remote_state=None,
        ),
    )


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


def test_state_from_status_renders_approved_draft_as_draft_only() -> None:
    revision = _open_revision(is_draft=True, review_decision="approved")

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=(revision,),
        )
    )

    assert "draft" in rendered
    assert "approved" not in rendered


def test_state_from_status_renders_changes_requested_draft_as_draft_only() -> None:
    revision = _open_revision(is_draft=True, review_decision="changes_requested")

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=(revision,),
        )
    )

    assert "draft" in rendered
    assert "changes requested" not in rendered


def test_state_from_status_separates_drafts_from_open_published() -> None:
    revisions = (
        _open_revision(is_draft=True),
        _open_revision(is_draft=False, review_decision="approved"),
    )

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=revisions,
        )
    )

    assert "draft" in rendered
    assert "1 approved" in rendered


def test_state_from_status_reports_github_unavailable_on_remote_error() -> None:
    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error="boom",
            revisions=(),
        )
    )

    assert "GitHub unavailable" in rendered


def test_state_from_status_collapses_approved_label_when_all_open_are_approved() -> None:
    revisions = (
        _open_revision(review_decision="approved"),
        _open_revision(review_decision="approved"),
    )

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=revisions,
        )
    )

    assert "approved" in rendered
    assert "2 approved" not in rendered


def test_state_from_status_marks_stale_saved_pull_request_link() -> None:
    revision = _missing_revision()

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=(revision,),
        )
    )

    assert "stale link" in rendered


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
