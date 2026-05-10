from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from jj_review.commands import cleanup as cleanup_module
from jj_review.commands.cleanup import (
    CleanupAction,
    PreparedCleanup,
    PreparedRebase,
    StackCommentCleanupPlan,
    _plan_remote_branch_cleanup,
    _run_cleanup_async,
    _stream_rebase,
)
from jj_review.config import RepoConfig
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.status import PreparedStatus
from jj_review.state.operation_lock import OperationLock
from jj_review.state.store import ReviewStateStore


def _fake_operation_lock(recorded_paths: list[Path] | None = None) -> OperationLock:
    def record_journal_path(path: Path) -> None:
        if recorded_paths is not None:
            recorded_paths.append(path)

    return cast(
        OperationLock,
        SimpleNamespace(record_journal_path=record_journal_path),
    )


def test_stream_cleanup_apply_clears_cached_stack_comment_after_deletion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state = ReviewState.model_validate(
        {
            "changes": {
                "change-1": CachedChange(
                    bookmark="review/feature-1",
                    pr_number=1,
                    pr_state="closed",
                    navigation_comment_id=12,
                ).model_dump(exclude_none=True),
            }
        }
    )
    saved_states: list[ReviewState] = []
    deleted_comment_ids: list[int] = []
    recorded_journal_paths: list[Path] = []
    state_store = cast(
        ReviewStateStore,
        SimpleNamespace(
            list_operations=lambda: [],
            require_writable=lambda: tmp_path,
            save=saved_states.append,
        ),
    )
    prepared_cleanup = PreparedCleanup(
        config=RepoConfig(),
        dry_run=False,
        bookmark_states={},
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        jj_client=cast(JjClient, SimpleNamespace()),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        remote_context_loaded=True,
        operation_lock=_fake_operation_lock(recorded_journal_paths),
        state=state,
        state_store=state_store,
    )

    class FakeGithubClientContext:
        async def __aenter__(self):
            return SimpleNamespace(
                delete_issue_comment=lambda owner, repo, *, comment_id: _record_deleted_comment(
                    comment_id
                )
            )

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def _record_deleted_comment(comment_id: int) -> None:
        deleted_comment_ids.append(comment_id)

    monkeypatch.setattr(
        "jj_review.commands.cleanup.build_github_client",
        lambda **kwargs: FakeGithubClientContext(),
    )
    async def fake_plan_stack_comment_cleanup(**kwargs):
        return StackCommentCleanupPlan(
            actions=(
                CleanupAction(
                    kind="stack navigation comment",
                    body="delete stack navigation comment #12 from PR #1",
                    status="planned",
                ),
            ),
            comments=((12, "navigation"),),
        )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {change_id: None for change_id in kwargs["change_ids"]},
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._plan_stack_comment_cleanup",
        fake_plan_stack_comment_cleanup,
    )
    result = asyncio.run(
        _run_cleanup_async(
            on_action=None,
            prepared_cleanup=prepared_cleanup,
        )
    )

    assert deleted_comment_ids == [12]
    assert result.actions == (
        CleanupAction(
            kind="stack navigation comment",
            body="delete stack navigation comment #12 from PR #1",
            status="applied",
        ),
    )
    assert [
        saved_state.changes["change-1"].navigation_comment_id for saved_state in saved_states
    ] == [None, None]
    assert recorded_journal_paths


def _status_revision(
    *,
    change_id: str,
    number: int,
    pull_request_state: str,
    subject: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        cached_change=None,
        change_id=change_id,
        link_state="active",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=number,
                state=pull_request_state,
            ),
            state="closed" if pull_request_state == "merged" else "open",
        ),
        pull_request_base_ref=lambda: "main",
        pull_request_number=lambda: number,
        subject=subject,
    )


def test_stream_rebase_plans_rebase_for_survivor_above_merged_path_revision(
    monkeypatch,
) -> None:
    merged_revision = _status_revision(
        change_id="merged-change",
        number=1,
        pull_request_state="merged",
        subject="merged feature",
    )
    survivor_revision = _status_revision(
        change_id="survivor-change",
        number=2,
        pull_request_state="open",
        subject="survivor feature",
    )
    prepared_rebase = PreparedRebase(
        config=RepoConfig(),
        dry_run=True,
        operation_lock=_fake_operation_lock(),
        prepared_status=cast(
            PreparedStatus,
            SimpleNamespace(
                github_inspection_count=lambda: 2,
                github_repository=None,
                prepared=SimpleNamespace(
                    client=SimpleNamespace(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=1, pr_state="merged"),
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            ),
                        ),
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=2, pr_state="open"),
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.stream_status",
        lambda **kwargs: SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            incomplete=False,
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        ),
    )

    result = _stream_rebase(prepared_rebase=prepared_rebase)

    assert len(result.actions) == 1
    assert result.actions[0].kind == "rebase"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "planned"


def test_stream_rebase_applies_rebase_for_survivor_above_merged_path_revision(
    monkeypatch,
) -> None:
    rebase_calls: list[tuple[str, str]] = []

    class FakeClient:
        def resolve_revision(self, revset: str):
            if revset == "survivor-change":
                return SimpleNamespace(only_parent_commit_id=lambda: "merged-commit")
            raise AssertionError(f"unexpected revset: {revset}")

        def rebase_revision(self, *, source: str, destination: str) -> None:
            rebase_calls.append((source, destination))

    merged_revision = _status_revision(
        change_id="merged-change",
        number=1,
        pull_request_state="merged",
        subject="merged feature",
    )
    survivor_revision = _status_revision(
        change_id="survivor-change",
        number=2,
        pull_request_state="open",
        subject="survivor feature",
    )
    prepared_rebase = PreparedRebase(
        config=RepoConfig(),
        dry_run=False,
        operation_lock=_fake_operation_lock(),
        prepared_status=cast(
            PreparedStatus,
            SimpleNamespace(
                github_inspection_count=lambda: 2,
                github_repository=None,
                prepared=SimpleNamespace(
                    client=FakeClient(),
                    state_store=SimpleNamespace(
                        list_operations=lambda: [],
                        require_writable=lambda: Path("/tmp"),
                    ),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=1, pr_state="merged"),
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            ),
                        ),
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=2, pr_state="open"),
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.stream_status",
        lambda **kwargs: SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            incomplete=False,
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        ),
    )

    result = _stream_rebase(prepared_rebase=prepared_rebase)

    assert rebase_calls == [("survivor-change", "trunk-commit")]
    assert len(result.actions) == 1
    assert result.actions[0].kind == "rebase"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "applied"


def test_plan_remote_branch_cleanup_allows_delete_when_local_forget_is_planned() -> None:
    plan = _plan_remote_branch_cleanup(
        cleanup_user_bookmarks=False,
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=("commit-1",),
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
        ),
        prefix="bosullivan",
        cached_change=CachedChange(
            bookmark="bosullivan/feature-aaaaaaaa",
            pr_number=1,
        ),
        local_bookmark_forget_planned=True,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
    )

    assert plan is not None
    assert plan.action.status == "planned"
    assert plan.expected_remote_target == "commit-1"


def test_plan_remote_branch_cleanup_skips_records_without_saved_pr_number() -> None:
    plan = _plan_remote_branch_cleanup(
        cleanup_user_bookmarks=False,
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=(),
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
        ),
        prefix="bosullivan",
        cached_change=CachedChange(bookmark="bosullivan/feature-aaaaaaaa"),
        local_bookmark_forget_planned=True,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
    )

    assert plan is None


def test_plan_local_bookmark_cleanup_forgets_safe_review_bookmark() -> None:
    plan = cleanup_module._plan_local_bookmark_cleanup(
        cleanup_user_bookmarks=False,
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        prefix="bosullivan",
        cached_change=CachedChange(
            bookmark="bosullivan/feature-aaaaaaaa",
            last_submitted_commit_id="commit-1",
        ),
        stale_reason="local change is no longer reviewable",
    )

    assert plan is not None
    assert plan.kind == "local bookmark"
    assert plan.status == "planned"
    assert (
        plan.message
        == "forget bosullivan/feature-aaaaaaaa (local change is no longer reviewable)"
    )
