from pathlib import Path
from typing import cast

import pytest

from jj_stack.errors import EXIT_AMBIGUOUS, CliError
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.selection import (
    parse_comma_separated_flag_values,
    resolve_orphaned_pull_request,
    resolve_selected_revset,
)


def test_parse_comma_separated_flag_values_dedupes_keeping_first_occurrence_order() -> None:
    assert parse_comma_separated_flag_values(["alice,bob", "carol,bob", "alice"]) == [
        "alice",
        "bob",
        "carol",
    ]


def test_resolve_selected_revset_requires_explicit_selection() -> None:
    with pytest.raises(CliError, match="requires an explicit revision selection"):
        resolve_selected_revset(
            command_label="relink",
            require_explicit=True,
            revset=None,
        )


def test_resolve_orphaned_pull_request_uses_supported_stack_membership() -> None:
    state = ReviewState(
        review_identities={
            "change-1": _identity(
                head_ref="jj-stack/change-1",
                pr_number=17,
            )
        }
    )
    jj_client = _JjClientStub(
        _REPO_ROOT,
        revisions_by_change_id={
            "change-1": (
                _revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    parents=("left-parent", "right-parent"),
                ),
            ),
        },
    )

    assert resolve_orphaned_pull_request(
        jj_client=cast(JjClient, jj_client),
        pull_request_reference="17",
        state=state,
    ) == (17, "change-1")


def test_resolve_orphaned_pull_request_fails_closed_on_multiple_matches() -> None:
    state = ReviewState(
        review_identities={
            "change-1": _identity(pr_number=17),
            "change-2": _identity(pr_number=17),
        }
    )
    jj_client = _JjClientStub(_REPO_ROOT)

    with pytest.raises(
        CliError,
        match=r"PR #17 is claimed by multiple tracked records \(change-1, change-2\)\.",
    ) as excinfo:
        resolve_orphaned_pull_request(
            jj_client=cast(JjClient, jj_client),
            pull_request_reference="17",
            state=state,
        )
    assert "Discard an incorrect claim with unstack --local" in str(excinfo.value)
    assert excinfo.value.exit_code == EXIT_AMBIGUOUS


_REPO_ROOT = Path(__file__).resolve().parent


def _identity(
    *,
    head_ref: str = "jj-stack/change",
    pr_number: int,
) -> ReviewIdentity:
    return ReviewIdentity(
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref=head_ref,
    )


class _JjClientStub:
    def __init__(
        self,
        repo_root,
        *,
        remotes: tuple[GitRemote, ...] = (),
        revisions_by_change_id: dict[str, tuple[object, ...]] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self._remotes = remotes
        self._revisions_by_change_id = revisions_by_change_id or {}

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        return self._remotes

    def query_revisions_by_change_ids(self, change_ids):
        return {
            change_id: self._revisions_by_change_id.get(change_id, ()) for change_id in change_ids
        }


def _revision(
    *,
    change_id: str,
    commit_id: str,
    parents: tuple[str, ...] = ("parent",),
) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=commit_id,
        current_working_copy=False,
        description=f"{change_id} subject",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=parents,
    )
