from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

import jj_stack.ui as ui
from jj_stack.commands.unstack import (
    CloseAction,
    PreparedClose,
    _cleanup_revision,
    _close_revision_preflight_error,
    _CloseMutationRun,
    unstack,
)
from jj_stack.errors import UsageError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.change_status import ReviewChangeStatus
from jj_stack.review.status import ReviewStatusRevision

CHANGE_ID = "aaaaaaaaaaaaaaaa"
BOOKMARK = "review/feature-aaaaaaaa"


def test_unstack_rejects_orphans_without_cleanup_before_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr(
        "jj_stack.commands.unstack.bootstrap_context",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid options must not bootstrap the repository")
        ),
    )

    with pytest.raises(UsageError, match="orphans requires --cleanup"):
        unstack(
            cleanup=False,
            cli_args=JjCliArgs(),
            debug=False,
            dry_run=False,
            local=False,
            pull_request="orphans",
            repository=None,
            revset=None,
        )


@pytest.mark.parametrize(
    ("bookmark_state", "expected_action"),
    (
        pytest.param(
            BookmarkState(
                name=BOOKMARK,
                local_targets=("commit-1", "commit-2"),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            CloseAction(
                kind="local bookmark",
                status="blocked",
                body=t"cannot forget {ui.bookmark(BOOKMARK)} because it is conflicted",
            ),
            id="conflicted-local",
        ),
        pytest.param(
            BookmarkState(
                name=BOOKMARK,
                local_targets=("other-commit",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            CloseAction(
                kind="local bookmark",
                status="blocked",
                body=t"cannot forget {ui.bookmark(BOOKMARK)} because it already points to "
                t"a different revision",
            ),
            id="moved-local",
        ),
        pytest.param(
            BookmarkState(
                name=BOOKMARK,
                local_targets=("commit-1",),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("commit-1", "commit-2")),
                ),
            ),
            CloseAction(
                kind="remote branch",
                status="blocked",
                body=t"cannot delete {ui.bookmark(f'{BOOKMARK}@origin')} because the remote "
                t"bookmark is conflicted",
            ),
            id="conflicted-remote",
        ),
        pytest.param(
            BookmarkState(
                name=BOOKMARK,
                local_targets=("commit-1",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("other-commit",)),),
            ),
            CloseAction(
                kind="remote branch",
                status="blocked",
                body=t"cannot delete {ui.bookmark(f'{BOOKMARK}@origin')} because it already "
                t"points to a different revision",
            ),
            id="moved-remote",
        ),
    ),
)
def test_cleanup_revision_blocks_unsafe_bookmarks(
    bookmark_state: BookmarkState,
    expected_action: CloseAction,
) -> None:
    result = asyncio.run(_run_cleanup_revision(bookmark_state=bookmark_state))

    assert len(result.actions) == 1
    assert result.actions[0].kind == expected_action.kind
    assert result.actions[0].status == expected_action.status
    assert result.actions[0].message == expected_action.message
    assert result.jj_client.delete_calls == []
    assert result.jj_client.forget_calls == []


def test_unstack_blocks_saved_identity_from_another_repository() -> None:
    identity = _review_identity().model_copy(update={"repository_name": "other-repository"})
    revision = replace(_stub_revision(), review_identity=identity)
    action = _close_revision_preflight_error(
        change_status=ReviewChangeStatus(
            local="present",
            remote_branch="current",
            remote_branch_matches_commit=True,
            pr_lifecycle="open",
            pr_draft=False,
            pr_review_decision="none",
            saved_review_identity=True,
        ),
        revision=revision,
        run=_CloseMutationRun(
            commit_ids_by_change_id={CHANGE_ID: "commit-1"},
            current_state=ReviewState(
                review_identities={CHANGE_ID: identity},
                submitted_baselines={CHANGE_ID: SubmittedBaseline(commit_id="commit-1")},
            ),
            github_client=cast(GithubClient, SimpleNamespace()),
            initial_observation=None,
            planned_closed_pull_requests=set(),
            review_identities={CHANGE_ID: identity},
            prepared_close=cast(
                PreparedClose,
                SimpleNamespace(
                    cleanup=False,
                    dry_run=False,
                    prepared_status=SimpleNamespace(
                        github_repository=GithubRepoAddress(
                            host="github.test",
                            owner="octo-org",
                            repo="stacked-review",
                        ),
                    ),
                ),
            ),
            record_action=lambda action: None,
        ),
    )

    assert action is not None
    assert action.status == "blocked"
    assert "saved repository does not match" in action.message


class _CleanupResult:
    def __init__(self, actions: list[CloseAction], jj_client: _JjClientStub) -> None:
        self.actions = actions
        self.jj_client = jj_client


class _JjClientStub:
    def __init__(self, bookmark_state: BookmarkState) -> None:
        self.bookmark_state = bookmark_state
        self.delete_calls: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.forget_calls: list[str] = []

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        return self.bookmark_state

    def list_remote_branches(self, *, remote: str, patterns) -> dict[str, str]:
        remote_state = self.bookmark_state.remote_target(remote)
        if remote_state is None or remote_state.target is None:
            return {}
        return {self.bookmark_state.name: remote_state.target}

    def delete_remote_bookmarks(
        self,
        *,
        remote: str,
        deletions,
        fetch: bool = True,
    ) -> None:
        self.delete_calls.append((remote, tuple(deletions)))

    def forget_bookmarks(self, bookmarks) -> None:
        self.forget_calls.extend(bookmarks)


class _GithubClientStub:
    async def list_issue_comments(self, *, issue_number):
        return ()


def _prepared_close(*, jj_client: _JjClientStub) -> PreparedClose:
    return cast(
        PreparedClose,
        SimpleNamespace(
            dry_run=False,
            prepared_status=SimpleNamespace(
                prepared=SimpleNamespace(
                    client=cast(JjClient, jj_client),
                    remote=SimpleNamespace(name="origin"),
                )
            ),
        ),
    )


async def _run_cleanup_revision(*, bookmark_state: BookmarkState) -> _CleanupResult:
    actions: list[CloseAction] = []
    jj_client = _JjClientStub(bookmark_state)
    await _cleanup_revision(
        bookmark_state=bookmark_state,
        local_commit_id="commit-1",
        review_identity=_review_identity(),
        revision=_stub_revision(),
        run=_CloseMutationRun(
            commit_ids_by_change_id={},
            current_state=ReviewState(),
            github_client=cast(GithubClient, _GithubClientStub()),
            initial_observation=None,
            planned_closed_pull_requests=set(),
            review_identities={},
            prepared_close=_prepared_close(jj_client=jj_client),
            record_action=actions.append,
        ),
        submitted_baseline=SubmittedBaseline(commit_id="commit-1"),
    )
    return _CleanupResult(actions=actions, jj_client=jj_client)


def _review_identity() -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=1,
        head_owner="octo-org",
        head_ref=BOOKMARK,
    )


def _stub_revision() -> ReviewStatusRevision:
    return ReviewStatusRevision(
        bookmark=BOOKMARK,
        bookmark_source="saved",
        change_id=CHANGE_ID,
        commit_id="commit-1",
        local_divergent=False,
        pull_request_lookup=None,
        review_identity=_review_identity(),
        remote_state=None,
        submitted_baseline=SubmittedBaseline(commit_id="commit-1"),
        managed_comments_lookup=None,
        subject="feature",
    )
