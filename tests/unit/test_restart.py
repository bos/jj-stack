from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_stack.models.review_state import ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.restart import RestartedChange, restart_state_for_stack
from tests.support.review_state import make_review_identity
from tests.support.revision_helpers import make_revision


def test_restart_state_uses_saved_readable_stem_and_keeps_durable_pair_shadowed() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="renamed feature\n",
    )
    identity = make_review_identity(
        head_ref="review/original-feature-abcdefgh",
        pr_number=42,
    )
    baseline = SubmittedBaseline(commit_id="old-commit")

    result = restart_state_for_stack(
        bookmark_states={},
        remote_name="origin",
        stack=_stack(revision),
        state=ReviewState(
            review_identities={revision.change_id: identity},
            submitted_baselines={revision.change_id: baseline},
        ),
    )

    new_bookmark = "review/original-feature-fresh-pr42-abcdefgh"
    assert result.changed == (
        RestartedChange(
            change_id=revision.change_id,
            new_bookmark=new_bookmark,
            old_bookmark=identity.head_ref,
            old_pr_number=42,
            subject="renamed feature",
        ),
    )
    assert revision.change_id not in result.state.review_identities
    assert revision.change_id not in result.state.submitted_baselines
    assert result.restarted[0].identity == identity
    assert result.restarted[0].baseline == baseline


def test_restart_state_reuses_exact_interrupted_local_and_remote_candidate() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )
    identity = make_review_identity(
        head_ref="review/original-feature-abcdefgh",
        pr_number=42,
    )
    fresh_bookmark = "review/original-feature-fresh-pr42-abcdefgh"

    result = restart_state_for_stack(
        bookmark_states={
            fresh_bookmark: BookmarkState(
                name=fresh_bookmark,
                local_targets=(revision.commit_id,),
                remote_targets=(
                    RemoteBookmarkState(
                        remote="origin",
                        targets=(revision.commit_id,),
                    ),
                ),
            )
        },
        remote_name="origin",
        stack=_stack(revision),
        state=ReviewState(
            review_identities={revision.change_id: identity},
            submitted_baselines={revision.change_id: SubmittedBaseline(commit_id="old-commit")},
        ),
    )

    assert result.changed[0].new_bookmark == fresh_bookmark


def test_restart_state_rejects_malformed_saved_branch() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )

    with pytest.raises(CliError, match="does not match change"):
        restart_state_for_stack(
            bookmark_states={},
            remote_name="origin",
            stack=_stack(revision),
            state=ReviewState(
                review_identities={
                    revision.change_id: make_review_identity(
                        head_ref="other/original-feature-abcdefgh",
                        pr_number=42,
                    )
                },
                submitted_baselines={
                    revision.change_id: SubmittedBaseline(commit_id="old-commit")
                },
            ),
        )


@pytest.mark.parametrize("missing", ("identity", "baseline"))
def test_restart_state_requires_a_complete_saved_pair(missing: str) -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )
    identity = make_review_identity(
        head_ref="review/original-feature-abcdefgh",
        pr_number=42,
    )
    baseline = SubmittedBaseline(commit_id="old-commit")

    with pytest.raises(CliError, match="complete saved PR tracking"):
        restart_state_for_stack(
            bookmark_states={},
            remote_name="origin",
            stack=_stack(revision),
            state=ReviewState(
                review_identities=(
                    {} if missing == "identity" else {revision.change_id: identity}
                ),
                submitted_baselines=(
                    {} if missing == "baseline" else {revision.change_id: baseline}
                ),
            ),
        )


@pytest.mark.parametrize(
    "bookmark_state",
    (
        BookmarkState(
            name="review/original-feature-fresh-pr42-abcdefgh",
            local_targets=("other-commit",),
        ),
        BookmarkState(
            name="review/original-feature-fresh-pr42-abcdefgh",
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("other-commit",)),),
        ),
    ),
)
def test_restart_state_rejects_candidate_at_another_commit(
    bookmark_state: BookmarkState,
) -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )

    with pytest.raises(CliError, match="points elsewhere"):
        restart_state_for_stack(
            bookmark_states={bookmark_state.name: bookmark_state},
            remote_name="origin",
            stack=_stack(revision),
            state=ReviewState(
                review_identities={
                    revision.change_id: make_review_identity(
                        head_ref="review/original-feature-abcdefgh",
                        pr_number=42,
                    )
                },
                submitted_baselines={
                    revision.change_id: SubmittedBaseline(commit_id="old-commit")
                },
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
