from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.models.review_state import ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.restart import restart_state_for_stack
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
        stack=_stack(revision),
        state=ReviewState(
            review_identities={revision.change_id: identity},
            submitted_baselines={revision.change_id: baseline},
        ),
    )

    assert result.restarted[0].change_id == revision.change_id
    assert result.restarted[0].new_branch == "review/original-feature-fresh-pr42-abcdefgh"
    assert revision.change_id not in result.state.review_identities
    assert revision.change_id not in result.state.submitted_baselines
    assert result.restarted[0].identity == identity
    assert result.restarted[0].baseline == baseline


def test_restart_state_reuses_the_same_deterministic_branch_on_retry() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )
    identity = make_review_identity(
        head_ref="review/original-feature-abcdefgh",
        pr_number=42,
    )
    state = ReviewState(
        review_identities={revision.change_id: identity},
        submitted_baselines={revision.change_id: SubmittedBaseline(commit_id="old-commit")},
    )

    first = restart_state_for_stack(stack=_stack(revision), state=state)
    second = restart_state_for_stack(stack=_stack(revision), state=state)

    assert first.restarted[0].new_branch == "review/original-feature-fresh-pr42-abcdefgh"
    assert second.restarted == first.restarted


def test_restart_state_rejects_malformed_saved_branch() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )

    with pytest.raises(CliError, match="does not match change"):
        restart_state_for_stack(
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


def test_restart_state_rejects_branch_claimed_by_another_saved_review() -> None:
    revision = make_revision(
        commit_id="commit-1",
        change_id="abcdefghijk",
        description="feature one\n",
    )

    with pytest.raises(CliError, match="another saved review claims"):
        restart_state_for_stack(
            stack=_stack(revision),
            state=ReviewState(
                review_identities={
                    revision.change_id: make_review_identity(
                        head_ref="review/original-feature-abcdefgh",
                        pr_number=42,
                    ),
                    "otherchange": make_review_identity(
                        head_ref="review/original-feature-fresh-pr42-abcdefgh",
                        pr_number=99,
                    ),
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
