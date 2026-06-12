from __future__ import annotations

from jj_stack.config import RepoConfig
from jj_stack.formatting import short_change_id
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.restart import (
    RestartedChange,
    cached_change_needs_restart,
    restart_state_for_stack,
)
from tests.support.revision_helpers import make_revision


def test_restart_state_replaces_review_identity_with_fresh_managed_bookmark() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )
    cached_change = CachedChange(
        bookmark="review/old-feature",
        bookmark_ownership="external",
        last_submitted_commit_id="old-commit",
        last_submitted_stack_head_change_id="head-change",
        pr_number=42,
        pr_state="closed",
        navigation_comment_id=101,
    )
    state = ReviewState(changes={revision.change_id: cached_change})

    assert cached_change_needs_restart(cached_change)

    result = restart_state_for_stack(
        bookmark_states={},
        config=RepoConfig(),
        stack=_stack(revision),
        state=state,
    )

    restarted = result.state.changes[revision.change_id]
    new_bookmark = restarted.bookmark
    assert new_bookmark is not None
    assert new_bookmark == (
        f"review/feature-one-fresh-pr42-{short_change_id(revision.change_id)}"
    )
    assert restarted.bookmark_ownership == "managed"
    assert restarted.link_state == "active"
    assert not cached_change_needs_restart(restarted)
    assert not restarted.has_review_identity
    assert result.changed == (
        RestartedChange(
            change_id=revision.change_id,
            new_bookmark=new_bookmark,
            old_bookmark="review/old-feature",
            old_pr_number=42,
            subject="feature one",
        ),
    )


def _stack(revision: LocalRevision) -> LocalStack:
    trunk = make_revision(
        commit_id="trunk-commit",
        change_id="trunkchange",
        description="trunk\n",
    )
    return LocalStack(
        base_parent=trunk,
        base_parent_is_trunk_ancestor=True,
        head=revision,
        revisions=(revision,),
        selected_revset=revision.change_id,
        trunk=trunk,
    )
