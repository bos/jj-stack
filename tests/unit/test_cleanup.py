from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from jj_review.bootstrap import CommandContext
from jj_review.commands.cleanup import command as cleanup_module
from jj_review.commands.cleanup.command import (
    _apply_stack_comment_cleanup_action,
    _plan_remote_branch_cleanup,
    _run_cleanup_async,
)
from jj_review.commands.cleanup.rebase import _stream_rebase
from jj_review.commands.cleanup.shared import (
    CleanupAction,
    PreparedCleanup,
    PreparedRebase,
    StackCommentCleanupPlan,
)
from jj_review.config import RepoConfig
from jj_review.github.client import GithubClient
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.change_status import classify_review_change_without_pull_request
from jj_review.review.status import PreparedStatus
from jj_review.state.store import ReviewStateStore


def _fake_context(
    *,
    config: RepoConfig | None = None,
    jj_client: JjClient | None = None,
    state_store: ReviewStateStore | None = None,
) -> CommandContext:
    return cast(
        CommandContext,
        SimpleNamespace(
            config=RepoConfig() if config is None else config,
            jj_client=cast(JjClient, SimpleNamespace()) if jj_client is None else jj_client,
            state_store=(
                cast(ReviewStateStore, SimpleNamespace()) if state_store is None else state_store
            ),
        ),
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
    state_store = cast(
        ReviewStateStore,
        SimpleNamespace(
            require_writable=lambda: tmp_path,
            save=saved_states.append,
        ),
    )
    prepared_cleanup = PreparedCleanup(
        context=_fake_context(state_store=state_store),
        bookmark_states={},
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        remote_context_loaded=True,
        dry_run=False,
        state=state,
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
        "jj_review.commands.cleanup.command.build_github_client",
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
        "jj_review.commands.cleanup.command._stale_change_reasons",
        lambda **kwargs: {change_id: None for change_id in kwargs["change_ids"]},
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.command._plan_stack_comment_cleanup",
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


def test_stack_comment_cleanup_records_blocked_action_without_comment_target(
    tmp_path: Path,
) -> None:
    state_store = cast(
        ReviewStateStore,
        SimpleNamespace(
            require_writable=lambda: tmp_path,
            save=lambda _state: None,
        ),
    )
    prepared_cleanup = PreparedCleanup(
        context=_fake_context(state_store=state_store),
        bookmark_states={},
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        remote_context_loaded=True,
        dry_run=True,
        state=ReviewState(),
    )
    blocked_action = CleanupAction(
        kind="stack navigation comment",
        body="cannot delete stack navigation comments because GitHub reports multiple candidates",
        status="blocked",
    )
    recorded_actions: list[CleanupAction] = []

    asyncio.run(
        _apply_stack_comment_cleanup_action(
            comment_plan=StackCommentCleanupPlan(actions=(blocked_action,)),
            change_id="change-1",
            github_client=cast(GithubClient, SimpleNamespace()),
            github_repository=ParsedGithubRepo(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
            next_changes={},
            prepared_cleanup=prepared_cleanup,
            record_action=recorded_actions.append,
        )
    )

    assert recorded_actions == [blocked_action]


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
        commit_id=f"{change_id}-commit",
        link_state="active",
        local_divergent=False,
        managed_comments_lookup=None,
        pull_request_lookup=SimpleNamespace(
            message=None,
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                is_draft=False,
                number=number,
                state=pull_request_state,
            ),
            repository_error=None,
            review_decision=None,
            review_decision_error=None,
            source="head",
            state="closed" if pull_request_state == "merged" else "open",
        ),
        remote_state=None,
        pull_request_base_ref=lambda: "main",
        pull_request_number=lambda: number,
        subject=subject,
    )


def test_stream_rebase_blocks_survivor_rebase_onto_another_survivor(
    monkeypatch,
) -> None:
    first_survivor_revision = _status_revision(
        change_id="first-survivor-change",
        number=1,
        pull_request_state="open",
        subject="first survivor",
    )
    merged_revision = _status_revision(
        change_id="merged-change",
        number=2,
        pull_request_state="merged",
        subject="merged feature",
    )
    second_survivor_revision = _status_revision(
        change_id="second-survivor-change",
        number=3,
        pull_request_state="open",
        subject="second survivor",
    )
    prepared_rebase = PreparedRebase(
        context=_fake_context(),
        dry_run=False,
        prepared_status=cast(
            PreparedStatus,
            SimpleNamespace(
                github_inspection_count=lambda: 3,
                github_repository=None,
                prepared=SimpleNamespace(
                    client=SimpleNamespace(),
                    state_store=SimpleNamespace(
                        require_writable=lambda: Path("/tmp"),
                    ),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=1, pr_state="open"),
                            revision=SimpleNamespace(
                                change_id="first-survivor-change",
                                commit_id="first-survivor-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            ),
                        ),
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=2, pr_state="merged"),
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "first-survivor-commit",
                            ),
                        ),
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=3, pr_state="open"),
                            revision=SimpleNamespace(
                                change_id="second-survivor-change",
                                commit_id="second-survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.rebase.stream_status",
        lambda **kwargs: SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            incomplete=False,
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(
                first_survivor_revision,
                merged_revision,
                second_survivor_revision,
            ),
            selected_revset="@",
        ),
    )

    result = _stream_rebase(prepared_rebase=prepared_rebase)

    assert len(result.actions) == 1
    assert result.blocked is True
    assert result.actions[0].kind == "rebase"
    assert result.actions[0].status == "blocked"
    assert "cannot automatically rebase second survivor" in result.actions[0].message
    assert "onto surviving change first-su" in result.actions[0].message


def test_plan_remote_branch_cleanup_allows_delete_when_local_forget_is_planned() -> None:
    remote_state = RemoteBookmarkState(remote="origin", targets=("commit-1",))
    cached_change = CachedChange(
        bookmark="bosullivan/feature-aaaaaaaa",
        pr_number=1,
    )
    plan = _plan_remote_branch_cleanup(
        cleanup_user_bookmarks=False,
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=("commit-1",),
            remote_targets=(remote_state,),
        ),
        prefix="bosullivan",
        cached_change=cached_change,
        local_bookmark_forget_planned=True,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_state=remote_state,
        review_status=classify_review_change_without_pull_request(
            cached_change=cached_change,
            commit_id=None,
            local="orphaned",
            remote_state=remote_state,
        ),
    )

    assert plan is not None
    assert plan.action.status == "planned"
    assert plan.expected_remote_target == "commit-1"


def test_plan_remote_branch_cleanup_skips_records_without_saved_pr_number() -> None:
    remote_state = RemoteBookmarkState(remote="origin", targets=("commit-1",))
    cached_change = CachedChange(bookmark="bosullivan/feature-aaaaaaaa")
    plan = _plan_remote_branch_cleanup(
        cleanup_user_bookmarks=False,
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=(),
            remote_targets=(remote_state,),
        ),
        prefix="bosullivan",
        cached_change=cached_change,
        local_bookmark_forget_planned=True,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_state=remote_state,
        review_status=classify_review_change_without_pull_request(
            cached_change=cached_change,
            commit_id=None,
            local="orphaned",
            remote_state=remote_state,
        ),
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
