from __future__ import annotations

from jj_stack.config import RepoConfig
from jj_stack.formatting import short_change_id
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.review_state import ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.restart import (
    RestartedChange,
    restart_state_for_stack,
)
from tests.support.review_state import make_review_identity
from tests.support.revision_helpers import make_revision


def test_restart_state_retires_review_pair_and_selects_fresh_bookmark() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )
    identity = make_review_identity(
        head_ref="review/old-feature",
        bookmark_ownership="external",
        pr_number=42,
    )
    baseline = SubmittedBaseline(commit_id="old-commit")
    state = ReviewState(
        review_identities={revision.change_id: identity},
        submitted_baselines={revision.change_id: baseline},
    )

    result = restart_state_for_stack(
        bookmark_states={},
        config=RepoConfig(),
        stack=_stack(revision),
        state=state,
    )

    new_bookmark = result.changed[0].new_bookmark
    assert new_bookmark == (
        f"review/feature-one-fresh-pr42-{short_change_id(revision.change_id)}"
    )
    assert revision.change_id not in result.state.review_identities
    assert revision.change_id not in result.state.submitted_baselines
    assert result.changed == (
        RestartedChange(
            change_id=revision.change_id,
            new_bookmark=new_bookmark,
            old_bookmark="review/old-feature",
            old_pr_number=42,
            subject="feature one",
        ),
    )


def test_restart_state_reuses_an_interrupted_fresh_local_bookmark() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )
    identity = make_review_identity(head_ref="review/old-feature", pr_number=42)
    baseline = SubmittedBaseline(commit_id="old-commit")
    fresh_bookmark = f"review/feature-one-fresh-pr42-{short_change_id(revision.change_id)}"

    result = restart_state_for_stack(
        bookmark_states={
            fresh_bookmark: BookmarkState(
                name=fresh_bookmark,
                local_targets=(revision.commit_id,),
            )
        },
        config=RepoConfig(),
        stack=_stack(revision),
        state=ReviewState(
            review_identities={revision.change_id: identity},
            submitted_baselines={revision.change_id: baseline},
        ),
    )

    assert result.changed[0].new_bookmark == fresh_bookmark


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
