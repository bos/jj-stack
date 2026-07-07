from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.submit.auto_close import (
    verify_no_unexpected_pull_request_closures,
)
from jj_stack.commands.submit.command import _resolve_submit_options
from jj_stack.commands.submit.inputs import (
    preflight_private_commits as _preflight_private_commits,
)
from jj_stack.commands.submit.models import (
    LocalBookmarkAction,
    PreparedSubmitRevision,
    PushOperation,
    SubmitMutationRun,
    SubmitOptions,
)
from jj_stack.commands.submit.pull_requests import (
    _ensure_pull_request_link_is_consistent,
    _reviewers_to_re_request,
    _select_discovered_pull_request,
)
from jj_stack.commands.submit.revisions import (
    _ClassifiedRevision,
    _ensure_remote_can_be_updated,
    _preflight_atomic_remote_push_plan,
    _resolve_local_action,
    prepare_submit_revisions as _prepare_submit_revisions,
    sync_local_bookmarks as _sync_local_bookmarks,
)
from jj_stack.config import AppConfig
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.github import (
    GithubBranchRef,
    GithubPullRequest,
    GithubPullRequestReview,
    GithubPullRequestReviewUser,
)
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.bookmarks import (
    BookmarkResolutionResult,
    BookmarkSource,
    ResolvedBookmark,
)
from jj_stack.review.change_status import (
    classify_review_change_without_pull_request,
    classify_saved_review_change,
)
from jj_stack.state.journal import OperationJournal
from jj_stack.state.store import ReviewStateStore
from tests.support.revision_helpers import make_revision


def _submit_options(*, dry_run: bool = False) -> SubmitOptions:
    return SubmitOptions(
        descriptions=(),
        describe_with=None,
        draft_mode="default",
        dry_run=dry_run,
        edit=False,
        labels=None,
        re_request=False,
        restart=False,
        reviewers=None,
        revset="@",
        team_reviewers=None,
        use_bookmarks=None,
    )


def test_resolve_local_action_rejects_conflicted_bookmark() -> None:
    with pytest.raises(
        CliError,
        match="2 conflicting local targets",
    ):
        _resolve_local_action("review/foo", ("abc123", "def456"), "abc123")


def _classified_revision(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    cached_change: CachedChange | None,
    commit_id: str,
    remote_state: RemoteBookmarkState | None,
) -> _ClassifiedRevision:
    return _ClassifiedRevision(
        bookmark=bookmark,
        bookmark_source=bookmark_source,
        bookmark_state=BookmarkState(name=bookmark),
        cached_change=cached_change,
        remote_state=remote_state,
        review_status=classify_review_change_without_pull_request(
            cached_change=cached_change,
            commit_id=commit_id,
            remote_state=remote_state,
        ),
        revision=make_revision(
            commit_id=commit_id,
            change_id="change-a",
            description=f"{bookmark}\n",
        ),
    )


def test_ensure_remote_can_be_updated_rejects_conflicted_remote_bookmark() -> None:
    with pytest.raises(
        CliError,
        match="Remote bookmark review/foo@origin is conflicted",
    ):
        _ensure_remote_can_be_updated(
            _classified_revision(
                bookmark="review/foo",
                bookmark_source="saved",
                cached_change=CachedChange(bookmark="review/foo"),
                commit_id="zzz999",
                remote_state=RemoteBookmarkState(
                    remote="origin",
                    targets=("abc123", "def456"),
                    tracking_targets=("abc123", "def456"),
                ),
            ),
            remote="origin",
        )


def test_ensure_remote_can_be_updated_rejects_unproven_existing_remote_branch() -> None:
    with pytest.raises(
        CliError,
        match="already exists and points elsewhere",
    ):
        _ensure_remote_can_be_updated(
            _classified_revision(
                bookmark="review/foo",
                bookmark_source="generated",
                cached_change=None,
                commit_id="def456",
                remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
            ),
            remote="origin",
        )


def test_ensure_remote_can_be_updated_allows_matching_untracked_remote_branch() -> None:
    _ensure_remote_can_be_updated(
        _classified_revision(
            bookmark="review/foo",
            bookmark_source="generated",
            cached_change=None,
            commit_id="abc123",
            remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
        ),
        remote="origin",
    )


def test_prepare_submit_revisions_preflights_remote_drift_before_local_bookmark_moves() -> None:
    first_revision = make_revision(
        commit_id="commit-1",
        change_id="change-1",
        description="feature 1\n",
    )
    second_revision = make_revision(
        commit_id="commit-2",
        change_id="change-2",
        description="feature 2\n",
    )
    client = _FakeSubmitPreparationClient(
        remote_targets={
            "review/feature-1": "commit-1",
            "review/feature-2": "unexpected-commit",
        }
    )

    with pytest.raises(CliError, match="unexpected commit"):
        _prepare_submit_revisions(
            bookmark_result=BookmarkResolutionResult(
                changed=False,
                resolutions=(
                    ResolvedBookmark(
                        bookmark="review/feature-1",
                        change_id="change-1",
                        source="saved",
                    ),
                    ResolvedBookmark(
                        bookmark="review/feature-2",
                        change_id="change-2",
                        source="saved",
                    ),
                ),
                state=ReviewState(
                    changes={
                        "change-1": CachedChange(
                            bookmark="review/feature-1",
                            last_submitted_commit_id="commit-1",
                        ),
                        "change-2": CachedChange(
                            bookmark="review/feature-2",
                            last_submitted_commit_id="commit-2",
                        ),
                    }
                ),
            ),
            bookmark_states={
                "review/feature-1": BookmarkState(
                    name="review/feature-1",
                    remote_targets=(
                        RemoteBookmarkState(
                            remote="origin",
                            targets=("commit-1",),
                            tracking_targets=("commit-1",),
                        ),
                    ),
                ),
                "review/feature-2": BookmarkState(
                    local_targets=("commit-2",),
                    name="review/feature-2",
                    remote_targets=(
                        RemoteBookmarkState(
                            remote="origin",
                            targets=("commit-2",),
                            tracking_targets=("commit-2",),
                        ),
                    ),
                ),
            },
            client=cast(JjClient, client),
            remote=GitRemote(name="origin", url="https://github.test/octo-org/repo.git"),
            stack=_local_stack(first_revision, second_revision),
        )

    assert client.set_bookmark_calls == []


def test_prepare_submit_revisions_rejects_non_atomic_push_before_bookmark_moves() -> None:
    first_revision = make_revision(
        commit_id="commit-1",
        change_id="change-1",
        description="feature 1\n",
    )
    second_revision = make_revision(
        commit_id="commit-2",
        change_id="change-2",
        description="feature 2\n",
    )
    client = _FakeSubmitPreparationClient(remote_targets={})

    with pytest.raises(CliError, match="not tracked locally"):
        _prepare_submit_revisions(
            bookmark_result=BookmarkResolutionResult(
                changed=False,
                resolutions=(
                    ResolvedBookmark(
                        bookmark="review/feature-1",
                        change_id="change-1",
                        source="saved",
                    ),
                    ResolvedBookmark(
                        bookmark="review/feature-2",
                        change_id="change-2",
                        source="saved",
                    ),
                ),
                state=ReviewState(
                    changes={
                        "change-1": CachedChange(bookmark="review/feature-1"),
                        "change-2": CachedChange(bookmark="review/feature-2"),
                    }
                ),
            ),
            bookmark_states={
                "review/feature-1": BookmarkState(
                    local_targets=("old-commit-1",),
                    name="review/feature-1",
                ),
                "review/feature-2": BookmarkState(
                    local_targets=("old-commit-2",),
                    name="review/feature-2",
                    remote_targets=(
                        RemoteBookmarkState(remote="origin", targets=("old-commit-2",)),
                    ),
                ),
            },
            client=cast(JjClient, client),
            remote=GitRemote(name="origin", url="https://github.test/octo-org/repo.git"),
            stack=_local_stack(first_revision, second_revision),
        )

    assert client.set_bookmark_calls == []


def test_preflight_atomic_remote_push_plan_allows_one_untracked_remote_update() -> None:
    _preflight_atomic_remote_push_plan(
        prepared_revisions=(
            _prepared_revision("review/feature-1", "commit-1", "git_update"),
        ),
        remote=GitRemote(name="origin", url="https://github.test/octo-org/repo.git"),
    )


def test_pull_request_link_rejects_missing_discovered_pull_request() -> None:
    cached_change = CachedChange(
        bookmark="review/foo",
        pr_number=17,
        pr_url="https://github.test/octo-org/repo/pull/17",
    )
    with pytest.raises(
        CliError,
        match="Saved pull request link exists",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            cached_change=cached_change,
            change_id="change-17",
            discovered_pull_request=None,
            saved_status=classify_saved_review_change(cached_change, local="present"),
        )


def test_pull_request_link_rejects_mismatched_pull_request_number() -> None:
    cached_change = CachedChange(bookmark="review/foo", pr_number=17)
    with pytest.raises(
        CliError,
        match="Saved pull request #17 does not match",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            cached_change=cached_change,
            change_id="change-17",
            discovered_pull_request=_github_pull_request(number=21),
            saved_status=classify_saved_review_change(cached_change, local="present"),
        )


def test_sync_local_bookmarks_allows_same_change_sideways_move_only() -> None:
    client = _FakeSubmitMutationClient(
        local_target_revisions={
            "old-commit-1": make_revision(
                commit_id="old-commit-1",
                change_id="change-1",
                description="old feature 1\n",
            ),
            "old-commit-2": make_revision(
                commit_id="old-commit-2",
                change_id="other-change",
                description="old feature 2\n",
            ),
        }
    )

    _sync_local_bookmarks(
        bookmark_result=BookmarkResolutionResult(
            changed=False,
            resolutions=(),
            state=ReviewState(),
        ),
        bookmark_states={
            "review/feature-1": BookmarkState(
                local_targets=("old-commit-1",),
                name="review/feature-1",
            ),
            "review/feature-2": BookmarkState(
                local_targets=("old-commit-2",),
                name="review/feature-2",
            ),
            "review/feature-3": BookmarkState(
                local_targets=("old-missing-commit",),
                name="review/feature-3",
            ),
        },
        client=cast(JjClient, client),
        prepared_revisions=(
            _prepared_revision(
                "review/feature-1",
                "new-commit-1",
                "batch",
                change_id="change-1",
                local_action="moved",
            ),
            _prepared_revision(
                "review/feature-2",
                "new-commit-2",
                "batch",
                change_id="change-2",
                local_action="moved",
            ),
            _prepared_revision(
                "review/feature-3",
                "new-commit-3",
                "batch",
                change_id="change-3",
                local_action="moved",
            ),
        ),
        run=SubmitMutationRun(
            dry_run=False,
            journal=OperationJournal.disabled(),
            state=ReviewState(),
            state_changes={},
            state_store=cast(ReviewStateStore, object()),
        ),
    )

    assert client.set_bookmark_calls == [
        ("review/feature-1", "new-commit-1", True),
        ("review/feature-2", "new-commit-2", False),
        ("review/feature-3", "new-commit-3", False),
    ]


class _FakeJjClientWithPrivateCommits:
    def __init__(self, private_revisions: tuple[LocalRevision, ...]) -> None:
        self._private_revisions = private_revisions

    def find_private_commits(
        self, revisions: tuple[LocalRevision, ...]
    ) -> tuple[LocalRevision, ...]:
        return self._private_revisions


class _FakeSubmitPreparationClient:
    def __init__(self, *, remote_targets: dict[str, str]) -> None:
        self._remote_targets = remote_targets
        self.set_bookmark_calls: list[tuple[str, str]] = []

    def list_remote_branches(
        self,
        *,
        remote: str,
        patterns: tuple[str, ...],
    ) -> dict[str, str]:
        return {
            pattern.removeprefix("refs/heads/"): self._remote_targets[
                pattern.removeprefix("refs/heads/")
            ]
            for pattern in patterns
            if pattern.removeprefix("refs/heads/") in self._remote_targets
        }

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        self.set_bookmark_calls.append((bookmark, revision))


class _FakeSubmitMutationClient:
    def __init__(self, *, local_target_revisions: dict[str, LocalRevision]) -> None:
        self._local_target_revisions = local_target_revisions
        self.set_bookmark_calls: list[tuple[str, str, bool]] = []

    def query_revisions(
        self,
        _revset: str,
    ) -> tuple[LocalRevision, ...]:
        return tuple(self._local_target_revisions.values())

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        self.set_bookmark_calls.append((bookmark, revision, allow_backwards))


def _local_stack(*revisions: LocalRevision) -> LocalStack:
    trunk = make_revision(
        commit_id="trunk",
        change_id="trunk-change",
        description="base\n",
    )
    return LocalStack(
        base_parent=trunk,
        head=revisions[-1],
        revisions=revisions,
        selected_revset=revisions[-1].change_id,
        trunk=trunk,
    )


def _prepared_revision(
    bookmark: str,
    commit_id: str,
    push_operation: PushOperation,
    *,
    change_id: str | None = None,
    local_action: LocalBookmarkAction = "unchanged",
) -> PreparedSubmitRevision:
    resolved_change_id = change_id or f"{commit_id}-change"
    return PreparedSubmitRevision(
        bookmark=bookmark,
        bookmark_source="saved",
        expected_remote_target="old-commit" if push_operation == "git_update" else None,
        local_action=local_action,
        push_operation=push_operation,
        remote_action="pushed",
        revision=make_revision(
            commit_id=commit_id,
            change_id=resolved_change_id,
            description=f"{bookmark}\n",
        ),
    )


def test_preflight_private_commits_raises_on_private_commit() -> None:
    private = make_revision(
        commit_id="head", change_id="head-change", description="private thing\n"
    )
    client = _FakeJjClientWithPrivateCommits((private,))

    with pytest.raises(CliError, match="git.private-commits"):
        _preflight_private_commits(client, (private,))


def test_select_discovered_pull_request_rejects_multiple_matches_for_head_branch() -> None:
    with pytest.raises(CliError, match="multiple pull requests"):
        _select_discovered_pull_request(
            head_label="octo-org:review/foo",
            pull_requests=(
                _github_pull_request(number=1),
                _github_pull_request(number=2),
            ),
        )


def test_select_discovered_pull_request_rejects_non_open_pull_request() -> None:
    with pytest.raises(CliError, match="in state closed"):
        _select_discovered_pull_request(
            head_label="octo-org:review/foo",
            pull_requests=(_github_pull_request(number=1, state="closed"),),
        )


def _reviews(*specs: tuple[int, str, str]) -> tuple[GithubPullRequestReview, ...]:
    return tuple(
        GithubPullRequestReview(
            id=review_id,
            state=state,
            user=GithubPullRequestReviewUser(login=login),
        )
        for review_id, login, state in specs
    )


def test_reviewers_to_re_request_includes_approved_and_changes_requested_by_id_order() -> None:
    reviews = _reviews(
        (2, "carol", "APPROVED"),
        (1, "bob", "CHANGES_REQUESTED"),
        (3, "dave", "COMMENTED"),
    )

    assert _reviewers_to_re_request(reviews) == ["bob", "carol"]


def test_reviewers_to_re_request_uses_latest_review_state_per_reviewer() -> None:
    reviews = _reviews(
        (1, "alice", "APPROVED"),
        (2, "alice", "DISMISSED"),
        (3, "erin", "CHANGES_REQUESTED"),
        (4, "erin", "APPROVED"),
    )

    assert _reviewers_to_re_request(reviews) == ["erin"]


class _RefetchPullRequestsClient:
    def __init__(self, *, refetched: dict[int, GithubPullRequest | None]) -> None:
        self._refetched = refetched

    async def get_pull_requests_by_numbers(
        self,
        *,
        pull_numbers,
    ) -> dict[int, GithubPullRequest | None]:
        return {number: self._refetched.get(number) for number in pull_numbers}


def test_verify_no_unexpected_pull_request_closures_raises_when_pr_vanishes() -> None:
    client = _RefetchPullRequestsClient(refetched={2: None})

    with pytest.raises(CliError, match="no longer reports them"):
        asyncio.run(
            verify_no_unexpected_pull_request_closures(
                discovered_pull_requests={"review/foo": _github_pull_request(number=2)},
                github_client=cast(GithubClient, client),
            )
        )


def test_verify_no_unexpected_pull_request_closures_raises_when_pr_becomes_closed() -> None:
    client = _RefetchPullRequestsClient(
        refetched={2: _github_pull_request(number=2, state="closed")},
    )

    with pytest.raises(CliError, match="closed by the end"):
        asyncio.run(
            verify_no_unexpected_pull_request_closures(
                discovered_pull_requests={"review/foo": _github_pull_request(number=2)},
                github_client=cast(GithubClient, client),
            )
        )


def _submit_context(config: AppConfig) -> CommandContext:
    return cast(CommandContext, SimpleNamespace(config=config))


def test_resolve_submit_options_prefers_cli_reviewers_and_labels_over_config() -> None:
    resolved = _resolve_submit_options(
        context=_submit_context(
            AppConfig(
                labels=["config-label"],
                reviewers=["config-user"],
                team_reviewers=["config-team"],
            )
        ),
        options=replace(
            _submit_options(),
            labels=["cli-label"],
            reviewers=["cli-user"],
        ),
    )

    assert resolved.labels == ["cli-label"]
    assert resolved.reviewers == ["cli-user"]
    assert resolved.team_reviewers == ["config-team"]


def test_resolve_submit_options_falls_back_to_config_reviewers_and_labels() -> None:
    resolved = _resolve_submit_options(
        context=_submit_context(
            AppConfig(labels=["config-label"], reviewers=["config-user"])
        ),
        options=replace(_submit_options(), labels=None, reviewers=None),
    )

    assert resolved.labels == ["config-label"]
    assert resolved.reviewers == ["config-user"]


def _github_pull_request(number: int, *, state: str = "open") -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        body="",
        head=GithubBranchRef(ref="review/foo"),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        number=number,
        state=state,
        title="feature",
    )
