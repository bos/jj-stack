from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

import jj_review.ui as ui
from jj_review.commands.close import (
    CloseAction,
    PreparedClose,
    _cleanup_revision,
    _CloseCleanupContext,
)
from jj_review.config import RepoConfig
from jj_review.github.client import GithubClient
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.jj.client import JjClient
from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_review.models.review_state import CachedChange
from jj_review.review.status import ReviewStatusRevision


def _stub_revision(*, change_id: str) -> ReviewStatusRevision:
    return ReviewStatusRevision(
        bookmark="",
        bookmark_source="generated",
        cached_change=None,
        change_id=change_id,
        commit_id="",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=None,
        remote_state=None,
        managed_comments_lookup=None,
        subject="",
    )


_GITHUB_REPO = ParsedGithubRepo(host="github.com", owner="octo-org", repo="stacked-review")


@pytest.mark.parametrize(
    ("bookmark_state", "expected_action"),
    [
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("commit-1", "commit-2"),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            CloseAction(
                kind="local bookmark",
                status="blocked",
                body=t"cannot forget {ui.bookmark('review/feature-aaaaaaaa')} because it is "
                t"conflicted",
            ),
            id="conflicted-local",
        ),
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("other-commit",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            CloseAction(
                kind="local bookmark",
                status="blocked",
                body=t"cannot forget {ui.bookmark('review/feature-aaaaaaaa')} because it "
                t"already points to a different revision",
            ),
            id="moved-local",
        ),
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("commit-1",),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("commit-1", "commit-2")),
                ),
            ),
            CloseAction(
                kind="remote branch",
                status="blocked",
                body=t"cannot delete {ui.bookmark('review/feature-aaaaaaaa@origin')} "
                t"because the remote bookmark is conflicted",
            ),
            id="conflicted-remote",
        ),
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("commit-1",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("other-commit",)),),
            ),
            CloseAction(
                kind="remote branch",
                status="blocked",
                body=t"cannot delete {ui.bookmark('review/feature-aaaaaaaa@origin')} "
                t"because it already points to a different revision",
            ),
            id="moved-remote",
        ),
    ],
)
def test_cleanup_revision_blocks_unsafe_bookmarks(
    bookmark_state: BookmarkState,
    expected_action: CloseAction,
) -> None:
    result = asyncio.run(_run_cleanup_revision(bookmark_state=bookmark_state))

    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.kind == expected_action.kind
    assert action.status == expected_action.status
    assert action.message == expected_action.message
    assert result.jj_client.delete_calls == []
    assert result.jj_client.forget_calls == []


class _CleanupResult:
    def __init__(self, actions: list[CloseAction], jj_client: _JjClientStub) -> None:
        self.actions = actions
        self.jj_client = jj_client


class _JjClientStub:
    def __init__(self) -> None:
        self.delete_calls: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.forget_calls: list[str] = []

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


def _prepared_close(
    *,
    cleanup_user_bookmarks: bool = False,
    jj_client: _JjClientStub,
) -> PreparedClose:
    return cast(
        PreparedClose,
        SimpleNamespace(
            context=SimpleNamespace(
                config=RepoConfig(
                    bookmark_prefix="review",
                    cleanup_user_bookmarks=cleanup_user_bookmarks,
                )
            ),
            dry_run=False,
            prepared_status=SimpleNamespace(
                prepared=SimpleNamespace(client=cast(JjClient, jj_client))
            ),
        ),
    )


async def _run_cleanup_revision(*, bookmark_state: BookmarkState) -> _CleanupResult:
    actions: list[CloseAction] = []
    jj_client = _JjClientStub()
    await _cleanup_revision(
        bookmark_state=bookmark_state,
        cached_change=CachedChange(bookmark="review/feature-aaaaaaaa"),
        commit_id="commit-1",
        context=_CloseCleanupContext(
            github_client=cast(GithubClient, SimpleNamespace()),
            github_repository=_GITHUB_REPO,
            next_changes={},
            prepared_close=_prepared_close(jj_client=jj_client),
            record_action=actions.append,
            remote_name="origin",
            revision=_stub_revision(change_id="aaaaaaaaaaaaaaaa"),
            revision_label="feature 1 (aaaaaaaa)",
        ),
    )
    return _CleanupResult(actions=actions, jj_client=jj_client)


def test_cleanup_revision_deletes_external_bookmark_when_configured() -> None:
    actions: list[CloseAction] = []
    jj_client = _JjClientStub()

    asyncio.run(
        _cleanup_revision(
            bookmark_state=BookmarkState(
                name="potato/feature-aaaaaaaa",
                local_targets=("commit-1",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            cached_change=CachedChange(
                bookmark="potato/feature-aaaaaaaa",
                bookmark_ownership="external",
            ),
            commit_id="commit-1",
            context=_CloseCleanupContext(
                github_client=cast(GithubClient, SimpleNamespace()),
                github_repository=_GITHUB_REPO,
                next_changes={},
                prepared_close=_prepared_close(
                    cleanup_user_bookmarks=True,
                    jj_client=jj_client,
                ),
                record_action=actions.append,
                remote_name="origin",
                revision=_stub_revision(change_id="aaaaaaaaaaaaaaaa"),
                revision_label="feature 1 (aaaaaaaa)",
            ),
        )
    )

    assert [action.kind for action in actions] == ["remote branch", "local bookmark"]
    assert jj_client.delete_calls == [("origin", (("potato/feature-aaaaaaaa", "commit-1"),))]
    assert jj_client.forget_calls == ["potato/feature-aaaaaaaa"]
