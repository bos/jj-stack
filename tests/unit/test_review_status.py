from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from jj_review.config import RepoConfig
from jj_review.errors import CliError, ErrorMessage
from jj_review.github.client import GithubClientError
from jj_review.github.error_messages import (
    summarize_github_lookup_error,
)
from jj_review.github.resolution import (
    ParsedGithubRepo,
)
from jj_review.jj import JjClient
from jj_review.models.bookmarks import GitRemote
from jj_review.models.github import GithubPullRequest
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review import status as status_module
from jj_review.review.status import (
    PreparedStack,
    PreparedStatus,
    ReviewStatusRevision,
    pinned_bookmarks_for_revisions,
    prepare_stack_for_status,
    stream_status_async,
)
from jj_review.state.store import ReviewStateStore


def test_stream_status_streams_local_fallback_revisions_after_github_abort(
    monkeypatch,
) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        outstanding_intents=(),
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
        stale_intents=(),
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
    github_status_calls: list[tuple[str | None, ErrorMessage | None]] = []
    streamed_revisions: list[tuple[str, bool]] = []

    async def fake_iter_status_revisions_with_github(**kwargs):
        if False:
            yield None
        raise CliError("jj bookmark list failed")

    monkeypatch.setattr(
        "jj_review.review.status._iter_status_revisions_with_github",
        fake_iter_status_revisions_with_github,
    )
    monkeypatch.setattr(
        "jj_review.review.status.build_status_revisions_for_prepared_stack",
        lambda prepared: local_only_revisions,
    )

    def on_github_status(
        github_repository: str | None,
        github_error: ErrorMessage | None,
    ) -> None:
        github_status_calls.append((github_repository, github_error))

    result = asyncio.run(
        stream_status_async(
            on_github_status=on_github_status,
            on_revision=lambda revision, github_available: streamed_revisions.append(
                (revision.change_id, github_available)
            ),
            prepared_status=prepared_status,
        )
    )

    assert github_status_calls == [("octo-org/stacked-review", None)]
    assert streamed_revisions == [
        ("bbbbbbbbbbbb", False),
        ("aaaaaaaaaaaa", False),
    ]
    assert result.github_error == "jj bookmark list failed"
    assert result.github_repository == "octo-org/stacked-review"
    assert result.incomplete is True
    assert result.revisions == (
        local_only_revisions[1],
        local_only_revisions[0],
    )


def test_stream_status_reports_github_target_without_error_for_empty_stack() -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        outstanding_intents=(),
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                stack=SimpleNamespace(revisions=()),
                state=ReviewState(),
                status_revisions=(),
            ),
        ),
        selected_revset="main",
        stale_intents=(),
        base_parent_subject="base",
    )
    github_status_calls: list[tuple[str | None, ErrorMessage | None]] = []

    result = asyncio.run(
        stream_status_async(
            on_github_status=lambda github_repository, github_error: github_status_calls.append(
                (github_repository, github_error)
            ),
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert github_status_calls == [("octo-org/stacked-review", None)]
    assert result.github_error is None
    assert result.github_repository == "octo-org/stacked-review"
    assert result.incomplete is False
    assert result.revisions == ()


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
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        outstanding_intents=(),
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
        stale_intents=(),
        base_parent_subject="base",
    )
    monkeypatch.setattr(
        "jj_review.review.status.build_status_revisions_for_prepared_stack",
        lambda prepared: local_only_revisions,
    )

    async def fail_iter_status_revisions_with_github(**kwargs):
        if False:
            yield None
        raise AssertionError("unexpected GitHub inspection for untracked stack")

    monkeypatch.setattr(
        "jj_review.review.status._iter_status_revisions_with_github",
        fail_iter_status_revisions_with_github,
    )

    result = asyncio.run(
        stream_status_async(
            on_github_status=None,
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert result.github_error is None
    assert result.github_repository == "octo-org/stacked-review"
    assert result.incomplete is False
    assert result.revisions == local_only_revisions


def test_stream_status_skips_cache_update_when_operation_lock_is_busy(
    monkeypatch,
    tmp_path,
) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    status_revision = ReviewStatusRevision(
        bookmark="review/feature-1-aaaaaaaa",
        bookmark_source="generated",
        cached_change=CachedChange(bookmark="review/feature-1-aaaaaaaa", pr_number=1),
        change_id="aaaaaaaaaaaa",
        commit_id="commit-1",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=None,
        remote_state=None,
        managed_comments_lookup=None,
        subject="feature 1",
    )
    saved_states: list[ReviewState] = []
    prepared_status = PreparedStatus(
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        outstanding_intents=(),
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                stack=SimpleNamespace(revisions=()),
                state=ReviewState(),
                state_store=SimpleNamespace(
                    require_writable=lambda: tmp_path,
                    save=lambda state: saved_states.append(state),
                ),
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/feature-1-aaaaaaaa",
                        bookmark_source="generated",
                        cached_change=CachedChange(pr_number=1),
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
        stale_intents=(),
        base_parent_subject="base",
    )
    monkeypatch.setattr(
        "jj_review.review.status.build_status_revisions_for_prepared_stack",
        lambda prepared: (status_revision,),
    )

    async def fake_iter_status_revisions_with_github(**kwargs):
        yield status_revision

    monkeypatch.setattr(
        "jj_review.review.status._iter_status_revisions_with_github",
        fake_iter_status_revisions_with_github,
    )
    monkeypatch.setattr(
        "jj_review.review.status.try_acquire_operation_lock",
        lambda *args, **kwargs: None,
    )

    result = asyncio.run(
        stream_status_async(
            lock_cache_update=True,
            on_github_status=None,
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert result.cache_update_skipped is True
    assert saved_states == []


def test_summarize_github_lookup_error_preserves_transport_detail() -> None:
    error = GithubClientError("GitHub request failed: Connection refused")

    assert (
        summarize_github_lookup_error(action="pull request lookup", error=error)
        == "pull request lookup failed (Connection refused)"
    )


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
        async def get_pull_requests_by_head_refs(self, owner, repo, *, head_refs):
            assert (owner, repo) == ("octo-org", "stacked-review")
            assert head_refs == ("review/old-branch",)
            return {"review/old-branch": ()}

        async def get_pull_requests_by_numbers(self, owner, repo, *, pull_numbers):
            assert (owner, repo) == ("octo-org", "stacked-review")
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
            github_repository=SimpleNamespace(owner="octo-org", repo="stacked-review"),
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
    monkeypatch,
) -> None:
    first = LocalRevision(
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
    second = LocalRevision(
        change_id="bbbbbbbb5678",
        commit_id="commit-2",
        current_working_copy=False,
        description="feature 2",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("commit-1",),
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
        head=second,
        revisions=(first, second),
        selected_revset="@",
        trunk=trunk,
    )
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")

    class FakeClient:
        def __init__(self, repo_root) -> None:
            self.repo_root = repo_root
            self.list_calls: list[tuple[str, ...] | None] = []

        def discover_review_stack(self, revset, *, allow_divergent=False, allow_immutable=False):
            return stack

        def list_git_remotes(self):
            return (remote,)

        def fetch_remote(self, *, remote: str) -> None:
            pass

        def list_bookmark_states(self, bookmarks=None):
            self.list_calls.append(None if bookmarks is None else tuple(bookmarks))
            return {}

    class FakeStateStore:
        def __init__(self, state: ReviewState) -> None:
            self.state = state

        def load(self) -> ReviewState:
            return self.state

        def save(self, state: ReviewState) -> None:
            self.state = state

        def list_operations(self) -> list[object]:
            return []

    def build_client(saved_state: ReviewState) -> FakeClient:
        client = FakeClient(tmp_path)
        monkeypatch.setattr(
            "jj_review.review.status.ReviewStateStore.for_repo",
            lambda _: FakeStateStore(saved_state),
        )
        return client

    pinned_state = ReviewState(
        changes={
            "aaaaaaaa1234": CachedChange(bookmark="review/feature-1-aaaaaaaa"),
            "bbbbbbbb5678": CachedChange(bookmark="review/feature-2-bbbbbbbb"),
        }
    )
    client = build_client(pinned_state)
    _prepare_status_for_test(config=RepoConfig(), fetch_remote_state=False, jj_client=client)
    assert client.list_calls == [
        ("review/feature-1-aaaaaaaa", "review/feature-2-bbbbbbbb"),
    ]

    client = build_client(
        ReviewState(
            changes={"aaaaaaaa1234": CachedChange(bookmark="review/feature-1-aaaaaaaa")}
        )
    )
    _prepare_status_for_test(config=RepoConfig(), fetch_remote_state=False, jj_client=client)
    assert client.list_calls == [None]


def test_pull_request_lookup_uses_review_decision_from_head_lookup() -> None:
    lookup = status_module._pull_request_lookup_from_discovered(
        head_label="octo-org:review/open",
        pull_requests=(
            GithubPullRequest(
                base={"ref": "main"},
                head={"ref": "review/open"},
                html_url="https://github.test/octo-org/stacked-review/pull/2",
                number=2,
                review_decision="approved",
                state="open",
                title="open",
            ),
        ),
    )

    assert lookup.review_decision == "approved"


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
        config=RepoConfig(),
        jj_client=cast(JjClient, SimpleNamespace(list_bookmark_states=lambda _: {})),
        persist_bookmarks=False,
        remote=None,
        remote_error=None,
        stack=stack,
        state=ReviewState(),
        state_store=cast(ReviewStateStore, state_store),
    )

    assert state_store.saved_states == []
    assert prepared.bookmark_result_changed is False
    assert prepared.state.changes == {}
    assert prepared.state_changes == {}
    assert prepared.status_revisions[0].bookmark_source == "generated"
    assert prepared.status_revisions[0].cached_change is None


def _prepare_status_for_test(
    *,
    config: RepoConfig,
    fetch_remote_state: bool,
    jj_client,
) -> PreparedStatus:
    from jj_review.jj import JjClient
    from jj_review.review.status import prepare_status

    return prepare_status(
        config=config,
        fetch_remote_state=fetch_remote_state,
        jj_client=cast(JjClient, jj_client),
        revset=None,
    )
