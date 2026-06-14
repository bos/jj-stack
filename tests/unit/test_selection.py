from pathlib import Path
from typing import cast

import pytest

from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_orphaned_pull_request,
    resolve_selected_revset,
)


def test_resolve_selected_revset_requires_explicit_selection() -> None:
    with pytest.raises(CliError, match="requires an explicit revision selection"):
        resolve_selected_revset(
            command_label="relink",
            require_explicit=True,
            revset=None,
        )


def test_resolve_linked_change_for_pull_request_uses_action_specific_guidance(
    monkeypatch,
) -> None:
    state = ReviewState(changes={"change-1": CachedChange(pr_number=17)})
    monkeypatch.setattr(
        "jj_stack.review.selection.ReviewStateStore.for_repo",
        lambda repo_root: _StateStoreStub(state),
    )
    jj_client = _JjClientStub(_REPO_ROOT, revisions_by_change_id={"change-1": ()})

    with pytest.raises(CliError, match="Close by revision once it is visible again."):
        resolve_linked_change_for_pull_request(
            action_name="close",
            jj_client=cast(JjClient, jj_client),
            pull_request_reference="17",
            revset=None,
        )


def test_resolve_orphaned_pull_request_uses_supported_stack_membership() -> None:
    state = ReviewState(
        changes={
            "change-1": CachedChange(
                bookmark="review/change-1",
                pr_number=17,
                pr_state="open",
                pr_url="https://example.test/pull/17",
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
        changes={
            "change-1": CachedChange(pr_number=17),
            "change-2": CachedChange(pr_number=17),
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
    assert "Repair the tracking data" in str(excinfo.value)


_REPO_ROOT = Path(__file__).resolve().parent


class _StateStoreStub:
    def __init__(self, state: ReviewState) -> None:
        self._state = state

    def load(self) -> ReviewState:
        return self._state


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
            change_id: self._revisions_by_change_id.get(change_id, ())
            for change_id in change_ids
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

