from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from jj_stack.bootstrap import CommandContext
from jj_stack.config import RepoConfig
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review import status as status_module
from jj_stack.review.status import (
    PreparedStack,
    PreparedStatus,
    ReviewStatusRevision,
    pinned_bookmarks_for_revisions,
    prepare_stack_for_status,
    stream_status_async,
)
from jj_stack.state.store import ReviewStateStore
from tests.support.revision_helpers import make_revision


def test_stream_status_streams_local_fallback_revisions_after_github_abort(
    monkeypatch,
) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_target=GithubTarget(
            remote=remote,
            repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
        ),
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                stack=SimpleNamespace(revisions=()),
                state=ReviewState(),
                status_revisions=(
                    SimpleNamespace(
                        cached_change=CachedChange(pr_number=1),
                        change_id="aaaaaaaaaaaa",
                    ),
                ),
            ),
        ),
        selected_revset="@",
        base_parent_subject="base",
    )
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            commit_id="commit-1",
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            remote_state=None,
            managed_comments_lookup=None,
            subject="feature 1",
        ),
        ReviewStatusRevision(
            bookmark="review/feature-2-bbbbbbbb",
            bookmark_source="generated",
            cached_change=None,
            change_id="bbbbbbbbbbbb",
            commit_id="commit-2",
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            remote_state=None,
            managed_comments_lookup=None,
            subject="feature 2",
        ),
    )
    streamed_revisions: list[tuple[str, bool]] = []

    async def fake_iter_status_revisions_with_github(**kwargs):
        if False:
            yield None
        raise CliError("jj bookmark list failed")

    monkeypatch.setattr(
        "jj_stack.review.status._iter_status_revisions_with_github",
        fake_iter_status_revisions_with_github,
    )
    monkeypatch.setattr(
        "jj_stack.review.status.build_status_revisions_for_prepared_stack",
        lambda prepared: local_only_revisions,
    )

    result = asyncio.run(
        stream_status_async(
            on_revision=lambda revision, github_available: streamed_revisions.append(
                (revision.change_id, github_available)
            ),
            prepared_status=prepared_status,
        )
    )

    assert streamed_revisions == [
        ("bbbbbbbbbbbb", False),
        ("aaaaaaaaaaaa", False),
    ]
    assert result.github_error == "jj bookmark list failed"
    assert result.github_repository == prepared_status.github_repository
    assert result.incomplete is True
    assert result.revisions == (
        local_only_revisions[1],
        local_only_revisions[0],
    )


def test_stream_status_skips_github_discovery_for_untracked_stack(monkeypatch) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            commit_id="commit-1",
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            remote_state=None,
            managed_comments_lookup=None,
            subject="feature 1",
        ),
    )
    prepared_status = PreparedStatus(
        github_target=GithubTarget(
            remote=remote,
            repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
        ),
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                stack=SimpleNamespace(revisions=()),
                state=ReviewState(),
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/feature-1-aaaaaaaa",
                        bookmark_source="generated",
                        cached_change=None,
                        revision=SimpleNamespace(
                            change_id="aaaaaaaaaaaa",
                            commit_id="commit-1",
                            subject="feature 1",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        base_parent_subject="base",
    )
    monkeypatch.setattr(
        "jj_stack.review.status.build_status_revisions_for_prepared_stack",
        lambda prepared: local_only_revisions,
    )

    async def fail_iter_status_revisions_with_github(**kwargs):
        if False:
            yield None
        raise AssertionError("unexpected GitHub inspection for untracked stack")

    monkeypatch.setattr(
        "jj_stack.review.status._iter_status_revisions_with_github",
        fail_iter_status_revisions_with_github,
    )

    result = asyncio.run(
        stream_status_async(
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert result.github_error is None
    assert result.github_repository == prepared_status.github_repository
    assert result.incomplete is False
    assert result.revisions == local_only_revisions


def test_locked_status_cache_update_merges_with_current_saved_state(tmp_path) -> None:
    state_store = ReviewStateStore(tmp_path / "state.json")
    prepared_state = ReviewState(
        changes={
            "aaaaaaaaaaaa": CachedChange(
                bookmark="review/feature-1-aaaaaaaa",
                pr_number=1,
                pr_review_decision="changes_requested",
                pr_state="open",
            )
        }
    )
    current_state = ReviewState(
        changes={
            "aaaaaaaaaaaa": CachedChange(
                bookmark="review/feature-1-aaaaaaaa",
                pr_number=1,
                pr_review_decision="changes_requested",
                pr_state="open",
            ),
            "bbbbbbbbbbbb": CachedChange(
                bookmark="review/other-bbbbbbbb",
                pr_number=99,
                pr_state="open",
            ),
        }
    )
    state_store.save(current_state)
    pull_request = GithubPullRequest(
        base={"ref": "main"},
        head={"ref": "review/feature-1-aaaaaaaa"},
        html_url="https://github.test/octo-org/stacked-review/pull/1",
        number=1,
        review_decision="approved",
        state="open",
        title="feature 1",
    )
    status_revision = ReviewStatusRevision(
        bookmark="review/feature-1-aaaaaaaa",
        bookmark_source="saved",
        cached_change=prepared_state.changes["aaaaaaaaaaaa"],
        change_id="aaaaaaaaaaaa",
        commit_id="commit-1",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=status_module.PullRequestLookup(
            message=None,
            pull_request=pull_request,
            review_decision="approved",
            review_decision_error=None,
            state="open",
        ),
        remote_state=None,
        managed_comments_lookup=None,
        subject="feature 1",
    )

    skipped = status_module._persist_status_cache_updates_with_optional_lock(
        lock_cache_update=True,
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                state=prepared_state,
                state_changes=dict(prepared_state.changes),
                state_store=state_store,
            ),
        ),
        revisions=(status_revision,),
    )

    saved = state_store.load()
    assert skipped is False
    assert saved.changes["aaaaaaaaaaaa"].pr_review_decision == "approved"
    assert saved.changes["bbbbbbbbbbbb"].pr_number == 99


def test_pinned_bookmarks_for_revisions_uses_cached_bookmarks_and_dedupes() -> None:
    first = cast(LocalRevision, SimpleNamespace(change_id="aaaaaaaa1234"))
    second = cast(LocalRevision, SimpleNamespace(change_id="bbbbbbbb5678"))
    third = cast(LocalRevision, SimpleNamespace(change_id="cccccccc9abc"))
    state = ReviewState(
        changes={
            "aaaaaaaa1234": CachedChange(bookmark="review/saved-aaaaaaaa"),
            "bbbbbbbb5678": CachedChange(bookmark="review/saved-bbbbbbbb"),
            "cccccccc9abc": CachedChange(bookmark="review/saved-aaaaaaaa"),
        }
    )

    result = pinned_bookmarks_for_revisions(
        revisions=(first, second, third),
        state=state,
    )

    assert result == ("review/saved-aaaaaaaa", "review/saved-bbbbbbbb")


def test_pull_request_lookup_falls_back_to_remembered_pr_number_when_branch_misses() -> None:
    class FakeGithubClient:
        repository = GithubRepoAddress(
            host="github.test",
            owner="octo-org",
            repo="stacked-review",
        )

        async def get_pull_requests_by_head_refs(self, *, head_refs):
            assert head_refs == ("review/old-branch",)
            return {"review/old-branch": ()}

        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            assert pull_numbers == (7,)
            return {
                7: GithubPullRequest.model_validate(
                    {
                        "base": {"ref": "review/base"},
                        "head": {
                            "label": "octo-org:review/old-branch",
                            "ref": "review/old-branch",
                        },
                        "html_url": "https://github.test/octo-org/stacked-review/pull/7",
                        "merged_at": "2026-03-16T12:00:00Z",
                        "number": 7,
                        "state": "closed",
                        "title": "feature 7",
                    }
                )
            }

    prepared_revision = SimpleNamespace(
        bookmark="review/old-branch",
        cached_change=CachedChange(
            bookmark="review/old-branch",
            pr_number=7,
        ),
    )

    lookups = asyncio.run(
        status_module._discover_pull_request_lookups(
            github_client=cast(Any, FakeGithubClient()),
            prepared_revisions=cast(Any, (prepared_revision,)),
        )
    )

    lookup = lookups["review/old-branch"]
    assert lookup.source == "remembered"
    assert lookup.state == "closed"
    assert lookup.pull_request is not None
    assert lookup.pull_request.number == 7
    assert lookup.pull_request.state == "merged"


def test_prepare_status_narrows_bookmark_listing_when_all_revisions_are_pinned(
    tmp_path,
) -> None:
    first = make_revision(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    second = make_revision(
        commit_id="commit-2",
        description="feature 2",
        change_id="bbbbbbbb5678",
    )
    stack = _stack_for_status(first, second)

    pinned_state = ReviewState(
        changes={
            "aaaaaaaa1234": CachedChange(bookmark="review/feature-1-aaaaaaaa"),
            "bbbbbbbb5678": CachedChange(bookmark="review/feature-2-bbbbbbbb"),
        }
    )
    client = _PrepareStatusClient(stack)
    state_store = _StateStoreStub(pinned_state)
    _prepare_status_for_test(
        config=RepoConfig(),
        fetch_remote_state=False,
        jj_client=client,
        state_store=state_store,
    )
    assert client.list_calls == [
        ("review/feature-1-aaaaaaaa", "review/feature-2-bbbbbbbb"),
    ]

    client = _PrepareStatusClient(stack)
    state_store = _StateStoreStub(
        ReviewState(
            changes={"aaaaaaaa1234": CachedChange(bookmark="review/feature-1-aaaaaaaa")}
        )
    )
    _prepare_status_for_test(
        config=RepoConfig(),
        fetch_remote_state=False,
        jj_client=client,
        state_store=state_store,
    )
    assert client.list_calls == [None]


def test_prepare_status_reloads_saved_state_after_fetch() -> None:
    revision = make_revision(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    stack = _stack_for_status(revision)
    stale_state = ReviewState(
        changes={
            revision.change_id: CachedChange(bookmark="review/stale", pr_number=1)
        }
    )
    refreshed_state = ReviewState(
        changes={
            revision.change_id: CachedChange(bookmark="review/refreshed", pr_number=2)
        }
    )

    client = _PrepareStatusClient(stack)
    state_store = _StateStoreStub(stale_state, refreshed_state)

    prepared_status = _prepare_status_for_test(
        config=RepoConfig(),
        fetch_remote_state=True,
        jj_client=client,
        state_store=state_store,
    )

    assert state_store.loads == 2
    assert client.fetches == ["origin"]
    assert client.list_calls == [("review/refreshed",)]
    prepared_revision = prepared_status.prepared.status_revisions[0]
    assert prepared_revision.cached_change == refreshed_state.changes[revision.change_id]


def test_pull_request_lookup_ignores_draft_review_decision() -> None:
    lookup = status_module._pull_request_lookup_from_discovered(
        head_label="octo-org:review/draft",
        pull_requests=(
            GithubPullRequest(
                base={"ref": "main"},
                draft=True,
                head={"ref": "review/draft"},
                html_url="https://github.test/octo-org/stacked-review/pull/3",
                number=3,
                review_decision="approved",
                state="open",
                title="draft",
            ),
        ),
    )

    assert lookup.review_decision is None


def test_prepare_stack_for_status_does_not_persist_generated_bookmarks() -> None:
    revision = LocalRevision(
        change_id="aaaaaaaa1234",
        commit_id="commit-1",
        current_working_copy=False,
        description="feature 1",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("trunk-commit",),
    )
    trunk = LocalRevision(
        change_id="trunkchangeid",
        commit_id="trunk-commit",
        current_working_copy=False,
        description="base",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=True,
        parents=("root",),
    )
    stack = LocalStack(
        base_parent=trunk,
        head=revision,
        revisions=(revision,),
        selected_revset="@",
        trunk=trunk,
    )

    class FakeStateStore:
        def __init__(self) -> None:
            self.saved_states: list[ReviewState] = []

        def save(self, state: ReviewState) -> None:
            self.saved_states.append(state)

    state_store = FakeStateStore()
    prepared = prepare_stack_for_status(
        context=cast(
            CommandContext,
            SimpleNamespace(
                config=RepoConfig(),
                jj_client=cast(JjClient, SimpleNamespace(list_bookmark_states=lambda _: {})),
                state_store=cast(ReviewStateStore, state_store),
            ),
        ),
        persist_bookmarks=False,
        remote=None,
        remote_error=None,
        stack=stack,
        state=ReviewState(),
    )

    assert state_store.saved_states == []
    assert prepared.bookmark_result_changed is False
    assert prepared.state.changes == {}
    assert prepared.state_changes == {}
    assert prepared.status_revisions[0].bookmark_source == "generated"
    assert prepared.status_revisions[0].cached_change is None


_STATUS_REMOTE = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")


def _stack_for_status(*revisions: LocalRevision) -> LocalStack:
    trunk = make_revision(
        commit_id="trunk",
        description="base",
        change_id="trunkchangeid",
    )
    return LocalStack(
        base_parent=trunk,
        head=revisions[-1],
        revisions=tuple(revisions),
        selected_revset="@",
        trunk=trunk,
    )


class _PrepareStatusClient:
    def __init__(self, stack: LocalStack) -> None:
        self.fetches: list[str] = []
        self.list_calls: list[tuple[str, ...] | None] = []
        self._stack = stack

    def discover_review_stack(self, revset, *, allow_divergent=False, allow_immutable=False):
        return self._stack

    def list_git_remotes(self):
        return (_STATUS_REMOTE,)

    def fetch_remote(self, *, remote: str) -> None:
        self.fetches.append(remote)

    def list_bookmark_states(self, bookmarks=None):
        self.list_calls.append(None if bookmarks is None else tuple(bookmarks))
        return {}


class _StateStoreStub:
    def __init__(self, *states: ReviewState) -> None:
        self.loads = 0
        self._states = states

    def load(self) -> ReviewState:
        state = self._states[min(self.loads, len(self._states) - 1)]
        self.loads += 1
        return state

    def save(self, state: ReviewState) -> None:
        raise AssertionError("status preparation should not save state")


def _prepare_status_for_test(
    *,
    config: RepoConfig,
    fetch_remote_state: bool,
    jj_client,
    state_store,
) -> PreparedStatus:
    from jj_stack.jj.client import JjClient
    from jj_stack.review.status import prepare_status

    return prepare_status(
        context=cast(
            CommandContext,
            SimpleNamespace(
                config=config,
                jj_client=cast(JjClient, jj_client),
                state_store=cast(ReviewStateStore, state_store),
            ),
        ),
        fetch_remote_state=fetch_remote_state,
        revset=None,
    )
