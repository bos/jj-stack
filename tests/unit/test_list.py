from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import jj_stack.ui as ui
from jj_stack.commands.list_ import (
    _format_pull_request_summary,
    _prepare_repo_inspection_context,
    _status_fragments,
)
from jj_stack.config import RepoConfig
from jj_stack.github.resolution import GithubTarget
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.change_status import ReviewChangeStatus
from jj_stack.review.discovery import discover_connected_tracked_stacks, discover_tracked_stacks


def _change_status(
    *,
    pr_lifecycle: str = "none",
    pr_draft: bool | None = None,
    pr_review_decision: str = "none",
) -> ReviewChangeStatus:
    return ReviewChangeStatus(
        local="present",
        link="active",
        remote_branch="current",
        remote_branch_matches_commit=True,
        pr_lifecycle=cast(Any, pr_lifecycle),
        pr_draft=pr_draft,
        pr_review_decision=cast(Any, pr_review_decision),
    )


def _fragment_text(statuses: tuple[ReviewChangeStatus, ...]) -> str:
    return " | ".join(
        ui.plain_text(fragment)
        for fragment in _status_fragments(
            github_error=None,
            remote_error=None,
            statuses=statuses,
        )
    )


def test_format_pull_request_summary_is_empty_without_pull_requests() -> None:
    assert _format_pull_request_summary(()) == ""


def test_format_pull_request_summary_names_single_pull_request() -> None:
    assert _format_pull_request_summary((7,)) == "PR 7"


def test_format_pull_request_summary_counts_multiple_pull_requests() -> None:
    assert _format_pull_request_summary((1, 2, 3, 4, 5)) == "5 PRs"


def test_status_fragments_report_partial_approval_counts() -> None:
    statuses = (
        _change_status(pr_lifecycle="open", pr_draft=False, pr_review_decision="approved"),
        _change_status(pr_lifecycle="open", pr_draft=False, pr_review_decision="approved"),
        _change_status(pr_lifecycle="open", pr_draft=False, pr_review_decision="none"),
        _change_status(pr_lifecycle="open", pr_draft=False, pr_review_decision="none"),
    )

    text = _fragment_text(statuses)

    assert "2 approved" in text
    assert "2 open" in text


def test_status_fragments_collapse_to_bare_approved_when_all_open_prs_approved() -> None:
    statuses = (
        _change_status(pr_lifecycle="open", pr_draft=False, pr_review_decision="approved"),
        _change_status(pr_lifecycle="open", pr_draft=False, pr_review_decision="approved"),
    )

    text = _fragment_text(statuses)

    assert "approved" in text
    assert "2 approved" not in text
    assert "open" not in text


def test_status_fragments_report_cleanup_needed_for_merged_pull_request() -> None:
    text = _fragment_text((_change_status(pr_lifecycle="merged"),))

    assert "cleanup needed" in text
    assert "merged ancestor" not in text


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
