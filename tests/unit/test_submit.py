from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.submit.auto_close import (
    verify_no_unexpected_pull_request_closures,
)
from jj_stack.commands.submit.command import (
    _resolve_submit_options,
    _validate_restart_recovery_candidates,
    run_submit_async,
)
from jj_stack.commands.submit.inputs import (
    preflight_private_commits as _preflight_private_commits,
)
from jj_stack.commands.submit.models import (
    GeneratedDescription,
    LocalBookmarkAction,
    PendingPullRequestSync,
    PreparedSubmitInputs,
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
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.bookmarks import (
    BookmarkSource,
    ResolvedBookmark,
)
from jj_stack.review.change_status import (
    classify_review_change_without_pull_request,
    classify_saved_review_identity,
)
from jj_stack.review.restart import RestartedChange, RestartedReview
from jj_stack.state.store import ReviewStateStore
from tests.support.review_state import make_review_identity
from tests.support.revision_helpers import make_revision

_REMOTE_URL = "https://github.test/octo-org/repo.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)


def _submit_options(*, dry_run: bool = False) -> SubmitOptions:
    return SubmitOptions(
        descriptions=(),
        describe_with=None,
        draft_mode="default",
        dry_run=dry_run,
        edit=False,
        existing_only=False,
        labels=None,
        re_request=False,
        restart=False,
        reviewers=None,
        revset="@",
        team_reviewers=None,
    )


def test_submit_prepared_callback_runs_after_spinner_stops(monkeypatch) -> None:
    trunk = make_revision(
        commit_id="trunk",
        change_id="trunk-change",
        description="main\n",
    )
    stack = LocalStack(
        base_parent=trunk,
        head=trunk,
        revisions=(),
        selected_revset="@-",
        trunk=trunk,
    )
    client = cast(JjClient, SimpleNamespace(list_bookmark_states=lambda: {}))
    prepared_inputs = PreparedSubmitInputs(
        bookmark_states={},
        bookmark_resolutions=(),
        client=client,
        generated_pull_request_descriptions={},
        generated_stack_description=None,
        remote=_REMOTE,
        restarted_change_ids=frozenset(),
        restarted_reviews=(),
        stack=stack,
        state=ReviewState(),
    )
    context = cast(
        CommandContext,
        SimpleNamespace(
            config=AppConfig(),
            state_store=SimpleNamespace(),
        ),
    )
    active_spinners: list[str] = []

    @contextmanager
    def recording_spinner(*, description: str) -> Iterator[None]:
        active_spinners.append(description)
        try:
            yield
        finally:
            active_spinners.remove(description)

    selected_lines: list[tuple[str, str]] = []

    def record_selected_line(change_id: str, subject: str) -> None:
        assert "Preparing submit" not in active_spinners
        selected_lines.append((change_id, subject))

    monkeypatch.setattr(
        "jj_stack.commands.submit.command.prepare_submit_inputs",
        lambda **_kwargs: prepared_inputs,
    )
    monkeypatch.setattr(
        "jj_stack.commands.submit.command.console.spinner",
        recording_spinner,
    )

    result = asyncio.run(
        run_submit_async(
            context=context,
            on_prepared=record_selected_line,
            options=_submit_options(),
        )
    )

    assert selected_lines == [(trunk.change_id, trunk.subject)]
    assert result.selected_change_id == trunk.change_id


def test_restart_recovery_rejects_a_changed_candidate() -> None:
    prepared = _prepared_revision(
        "review/feature-fresh-pr17-abcdefgh",
        "new-commit",
        "batch",
        change_id="abcdefghijk",
    )

    with pytest.raises(CliError, match="Cannot recover the restart") as caught:
        _validate_restart_recovery_candidates(
            head_owner="octo-org",
            pending_syncs=(
                PendingPullRequestSync(
                    base_branch="main",
                    discovered_pull_request=_github_pull_request(number=18),
                    generated_description=GeneratedDescription(title="batch", body=""),
                    parent_change_id=None,
                    prepared=prepared,
                    stack_head_change_id=prepared.revision.change_id,
                ),
            ),
            remote_targets={prepared.bookmark: prepared.revision.commit_id},
            restarted_change_ids=frozenset({prepared.revision.change_id}),
        )

    assert caught.value.hint is not None
    assert "relink 18 abcdefgh" in str(caught.value.hint)


def test_restart_recovery_rejects_a_changed_remote_branch() -> None:
    prepared = _prepared_revision(
        "review/feature-fresh-pr17-abcdefgh",
        "new-commit",
        "batch",
        change_id="abcdefghijk",
    )
    pull_request = _github_pull_request(number=18).model_copy(
        update={
            "head": GithubBranchRef(
                label=f"octo-org:{prepared.bookmark}",
                ref=prepared.bookmark,
                sha=prepared.revision.commit_id,
            )
        }
    )

    with pytest.raises(CliError, match="replacement branch"):
        _validate_restart_recovery_candidates(
            head_owner="octo-org",
            pending_syncs=(
                PendingPullRequestSync(
                    base_branch="main",
                    discovered_pull_request=pull_request,
                    generated_description=GeneratedDescription(title="batch", body=""),
                    parent_change_id=None,
                    prepared=prepared,
                    stack_head_change_id=prepared.revision.change_id,
                ),
            ),
            remote_targets={prepared.bookmark: "other-commit"},
            restarted_change_ids=frozenset({prepared.revision.change_id}),
        )


def test_restart_submission_stages_then_replaces_the_exact_saved_pair() -> None:
    change_id = "abcdefghijk"
    old_identity = make_review_identity(
        head_ref="review/feature-abcdefgh",
        pr_number=17,
    )
    old_baseline = SubmittedBaseline(commit_id="old-commit")
    new_identity = make_review_identity(
        head_ref="review/feature-fresh-pr17-abcdefgh",
        pr_number=18,
    )
    new_baseline = SubmittedBaseline(commit_id="new-commit")
    replacement_state = ReviewState(
        review_identities={change_id: new_identity},
        submitted_baselines={change_id: new_baseline},
    )
    calls: list[dict[str, object]] = []

    class RecordingStore:
        def relink_reviews(self, **kwargs):
            calls.append(kwargs)
            return replacement_state

    restarted = RestartedReview(
        baseline=old_baseline,
        change=RestartedChange(
            change_id=change_id,
            new_bookmark=new_identity.head_ref,
            old_bookmark=old_identity.head_ref,
            old_pr_number=old_identity.pr_number,
            subject="feature",
        ),
        commit_id="new-commit",
        identity=old_identity,
    )
    run = SubmitMutationRun(
        dry_run=False,
        restarted_reviews={change_id: restarted},
        state=ReviewState(
            review_identities={change_id: old_identity},
            submitted_baselines={change_id: old_baseline},
        ),
        state_store=cast(ReviewStateStore, RecordingStore()),
    )

    run.record_submission(
        baseline=new_baseline,
        change_id=change_id,
        identity=new_identity,
    )
    assert calls == []
    assert run.state.review_identities[change_id] == old_identity

    run.commit_restart_submissions()

    assert calls == [
        {
            "expected": {change_id: (old_identity, old_baseline)},
            "replacements": {change_id: (new_identity, new_baseline)},
        }
    ]
    assert run.state == replacement_state


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
    commit_id: str,
    remote_state: RemoteBookmarkState | None,
    review_identity: ReviewIdentity | None,
    submitted_baseline: SubmittedBaseline | None = None,
) -> _ClassifiedRevision:
    return _ClassifiedRevision(
        bookmark=bookmark,
        bookmark_source=bookmark_source,
        bookmark_state=BookmarkState(name=bookmark),
        review_identity=review_identity,
        remote_state=remote_state,
        review_status=classify_review_change_without_pull_request(
            commit_id=commit_id,
            remote_state=remote_state,
            review_identity=review_identity,
        ),
        revision=make_revision(
            commit_id=commit_id,
            change_id="change-a",
            description=f"{bookmark}\n",
        ),
        submitted_baseline=submitted_baseline,
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
                commit_id="zzz999",
                remote_state=RemoteBookmarkState(
                    remote="origin",
                    targets=("abc123", "def456"),
                    tracking_targets=("abc123", "def456"),
                ),
                review_identity=make_review_identity(head_ref="review/foo"),
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
                commit_id="def456",
                remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
                review_identity=None,
            ),
            remote="origin",
        )


def test_ensure_remote_can_be_updated_allows_matching_untracked_remote_branch() -> None:
    _ensure_remote_can_be_updated(
        _classified_revision(
            bookmark="review/foo",
            bookmark_source="generated",
            commit_id="abc123",
            remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
            review_identity=None,
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
            bookmark_resolutions=(
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
            remote=_REMOTE,
            stack=_local_stack(first_revision, second_revision),
            state=ReviewState(
                review_identities={
                    "change-1": make_review_identity(head_ref="review/feature-1"),
                    "change-2": make_review_identity(head_ref="review/feature-2"),
                },
                submitted_baselines={
                    "change-1": SubmittedBaseline(commit_id="commit-1"),
                    "change-2": SubmittedBaseline(commit_id="commit-2"),
                },
            ),
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
            bookmark_resolutions=(
                ResolvedBookmark(
                    bookmark="review/feature-1",
                    change_id="change-1",
                    source="generated",
                ),
                ResolvedBookmark(
                    bookmark="review/feature-2",
                    change_id="change-2",
                    source="generated",
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
            remote=_REMOTE,
            stack=_local_stack(first_revision, second_revision),
            state=ReviewState(),
        )

    assert client.set_bookmark_calls == []


def test_preflight_atomic_remote_push_plan_allows_one_untracked_remote_update() -> None:
    _preflight_atomic_remote_push_plan(
        prepared_revisions=(_prepared_revision("review/feature-1", "commit-1", "git_update"),),
        remote=_REMOTE,
    )


def test_pull_request_link_rejects_missing_discovered_pull_request() -> None:
    review_identity = make_review_identity(head_ref="review/foo", pr_number=17)
    submitted_baseline = SubmittedBaseline(commit_id="commit-17")
    with pytest.raises(
        CliError,
        match="Saved pull request link exists",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            change_id="change-17",
            discovered_pull_request=None,
            review_identity=review_identity,
            saved_status=classify_saved_review_identity(
                review_identity,
                local="present",
            ),
            submitted_baseline=submitted_baseline,
        )


def test_pull_request_link_rejects_mismatched_pull_request_number() -> None:
    review_identity = make_review_identity(head_ref="review/foo", pr_number=17)
    with pytest.raises(
        CliError,
        match="Saved pull request #17 does not match",
    ):
        _ensure_pull_request_link_is_consistent(
            bookmark="review/foo",
            change_id="change-17",
            discovered_pull_request=_github_pull_request(number=21),
            review_identity=review_identity,
            saved_status=classify_saved_review_identity(
                review_identity,
                local="present",
            ),
            submitted_baseline=SubmittedBaseline(commit_id="commit-17"),
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
            restarted_reviews={},
            state=ReviewState(),
            state_store=cast(ReviewStateStore, object()),
        ),
        state=ReviewState(),
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
