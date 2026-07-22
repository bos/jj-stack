from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import jj_stack.commands.cleanup.command as cleanup_module
import jj_stack.commands.cleanup.stack_comments as stack_comments_module
import jj_stack.commands.cleanup.stale as stale_module
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._close_actions import ManagedCommentLookup
from jj_stack.commands.cleanup.command import (
    _apply_stale_cleanup_mutation_plans,
    _plan_remote_branch_cleanup,
)
from jj_stack.commands.cleanup.shared import (
    CleanupAction,
    PreparedCleanup,
    RemoteBranchCleanupPlan,
    _StaleCleanupMutationPlan,
)
from jj_stack.commands.cleanup.stack_comments import (
    StackCommentCleanupPlan,
    _apply_stack_comment_cleanup_action,
    _plan_stack_comment_cleanup,
)
from jj_stack.config import RepoConfig
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.github import GithubIssueComment
from jj_stack.models.review_state import (
    BookmarkOwnership,
    LinkState,
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
)
from jj_stack.review.change_status import classify_review_change_without_pull_request
from jj_stack.state.store import ReviewStateStore
from tests.support.revision_helpers import make_revision


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


def _review_identity(
    *,
    bookmark: str = "review/feature-aaaaaaaa",
    bookmark_ownership: BookmarkOwnership = "managed",
    link_state: LinkState = "active",
    pr_number: int = 1,
) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref=bookmark,
        bookmark_ownership=bookmark_ownership,
        link_state=link_state,
    )


def test_stack_comment_cleanup_blocked_plan_surfaces_action_without_github_deletes() -> None:
    """A blocked stack-comment plan surfaces the blocked action and deletes nothing.

    The github_client below is a bare SimpleNamespace with no methods: any
    comment-delete attempt would raise AttributeError, so the run completing is
    itself the proof that no GitHub deletes were performed.
    """

    prepared_cleanup = PreparedCleanup(
        context=_fake_context(),
        bookmark_states={},
        github_target=GithubTarget(
            remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
            repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
        ),
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
            github_client=cast(GithubClient, SimpleNamespace()),
            prepared_cleanup=prepared_cleanup,
            record_action=recorded_actions.append,
        )
    )

    assert recorded_actions == [blocked_action]
    assert recorded_actions[0].status == "blocked"


def test_stack_comment_cleanup_blocks_all_comment_deletes_when_one_lookup_blocks(
    monkeypatch,
) -> None:
    blocked_reason = "cannot delete stack overview comments because GitHub reports duplicates"

    async def fake_find_managed_comments(**kwargs):
        return (
            ManagedCommentLookup(
                kind="navigation",
                comment=GithubIssueComment(
                    body="managed navigation",
                    databaseId=12,
                    url="https://api.github.test/comments/12",
                ),
            ),
            ManagedCommentLookup(kind="overview", blocked_reason=blocked_reason),
        )

    monkeypatch.setattr(
        stack_comments_module,
        "_find_managed_comments",
        fake_find_managed_comments,
    )

    class FakeGithubClient:
        async def get_pull_request(self, *, pull_number):
            return SimpleNamespace(
                head=SimpleNamespace(
                    label="octo-org:review/other",
                    ref="review/other",
                )
            )

    plan = asyncio.run(
        _plan_stack_comment_cleanup(
            review_identity=_review_identity(
                bookmark="review/feature",
                link_state="unlinked",
                pr_number=1,
            ),
            github_client=cast(GithubClient, FakeGithubClient()),
        )
    )

    assert plan == StackCommentCleanupPlan(
        actions=(
            CleanupAction(
                kind="stack overview comment",
                body=blocked_reason,
                status="blocked",
            ),
        )
    )


def test_cleanup_command_exits_nonzero_when_cleanup_result_blocks(
    monkeypatch,
) -> None:
    prepared_cleanup = PreparedCleanup(
        context=_fake_context(),
        bookmark_states={},
        github_target=None,
        dry_run=False,
        state=ReviewState(),
    )
    blocked_action = CleanupAction(
        kind="stack navigation comment",
        body="cannot inspect stack navigation comments for PR #1",
        status="blocked",
    )

    async def fake_run_cleanup_async(**kwargs):
        return cleanup_module.CleanupResult(actions=(blocked_action,))

    monkeypatch.setattr(cleanup_module, "_prepare_cleanup", lambda **kwargs: prepared_cleanup)
    monkeypatch.setattr(cleanup_module, "_stale_change_reasons", lambda **kwargs: {})
    monkeypatch.setattr(cleanup_module, "_run_cleanup_async", fake_run_cleanup_async)

    assert (
        cleanup_module._run_cleanup_command(
            context=_fake_context(),
            dry_run=False,
        )
        == 1
    )


def test_stale_change_reasons_reports_changes_outside_supported_stacks(monkeypatch) -> None:
    live_revision = make_revision(
        change_id="live-change",
        commit_id="live-commit",
        description="live\n",
    )
    stale_revision = make_revision(
        change_id="stale-change",
        commit_id="stale-commit",
        description="stale\n",
    )

    class FakeJjClient:
        def query_revisions_by_change_ids(self, change_ids):
            assert change_ids == ("live-change", "stale-change")
            return {
                "live-change": (live_revision,),
                "stale-change": (stale_revision,),
            }

    def fake_discover_stacks_from_revisions(*, jj_client, revisions):
        return (SimpleNamespace(revisions=(live_revision,)),)

    monkeypatch.setattr(
        stale_module,
        "discover_stacks_from_revisions",
        fake_discover_stacks_from_revisions,
    )

    reasons = stale_module._stale_change_reasons(
        change_ids=("live-change", "stale-change"),
        context=_fake_context(jj_client=cast(JjClient, FakeJjClient())),
    )

    assert reasons["live-change"] is None
    assert reasons["stale-change"] == "local change no longer participates in a supported stack"


def test_orphan_local_bookmark_cleanup_keeps_supported_targets_only(monkeypatch) -> None:
    live_revision = make_revision(
        change_id="live-change",
        commit_id="live-commit",
        description="live\n",
    )
    stale_revision = make_revision(
        change_id="stale-change",
        commit_id="stale-commit",
        description="stale\n",
    )

    class FakeJjClient:
        def query_revisions_by_commit_ids(self, commit_ids):
            return (live_revision, stale_revision)

    def fake_discover_stacks_from_revisions(*, jj_client, revisions):
        return (SimpleNamespace(revisions=(live_revision,)),)

    monkeypatch.setattr(
        stale_module,
        "discover_stacks_from_revisions",
        fake_discover_stacks_from_revisions,
    )

    plans = stale_module._plan_orphan_local_bookmark_cleanups(
        bookmark_states={
            "other/live": BookmarkState(name="other/live", local_targets=("skip",)),
            "review/conflict": BookmarkState(
                name="review/conflict",
                local_targets=("left", "right"),
            ),
            "review/live": BookmarkState(
                name="review/live",
                local_targets=("live-commit",),
            ),
            "review/stale": BookmarkState(
                name="review/stale",
                local_targets=("stale-commit",),
            ),
        },
        context=_fake_context(jj_client=cast(JjClient, FakeJjClient())),
        tracked_bookmarks=set(),
    )

    actions_by_bookmark = {plan.bookmark: plan.action for plan in plans}
    assert actions_by_bookmark["review/conflict"].status == "blocked"
    assert actions_by_bookmark["review/stale"].status == "planned"
    assert "review/live" not in actions_by_bookmark
    assert "other/live" not in actions_by_bookmark


def test_plan_remote_branch_cleanup_allows_delete_when_local_forget_is_planned() -> None:
    remote_state = RemoteBookmarkState(remote="origin", targets=("commit-1",))
    review_identity = _review_identity(
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
        local_bookmark_forget_planned=True,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_state=remote_state,
        review_identity=review_identity,
        review_status=classify_review_change_without_pull_request(
            commit_id=None,
            local="orphaned",
            remote_state=remote_state,
            review_identity=review_identity,
        ),
    )

    assert plan is not None
    assert plan.action.status == "planned"
    assert plan.expected_remote_target == "commit-1"


def test_apply_stale_cleanup_batches_remote_deletes_then_forgets_then_fetches_once() -> None:
    calls: list[tuple[str, object]] = []

    class RecordingJjClient:
        def delete_remote_bookmarks(self, *, remote, deletions, fetch=True) -> None:
            calls.append(("delete_remote_bookmarks", (remote, tuple(deletions), fetch)))

        def forget_bookmarks(self, bookmarks) -> None:
            calls.append(("forget_bookmarks", tuple(bookmarks)))

        def fetch_remote(self, *, remote, branches=None) -> None:
            calls.append(("fetch_remote", (remote, branches)))

    prepared_cleanup = PreparedCleanup(
        context=_fake_context(jj_client=cast(JjClient, RecordingJjClient())),
        bookmark_states={},
        github_target=GithubTarget(
            remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
            repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
        ),
        dry_run=False,
        state=ReviewState(),
    )

    def mutation_plan(bookmark: str, expected_target: str) -> _StaleCleanupMutationPlan:
        return _StaleCleanupMutationPlan(
            local_bookmark_action=CleanupAction(
                kind="local bookmark",
                status="planned",
                body=f"forget {bookmark}",
            ),
            remote_plan=RemoteBranchCleanupPlan(
                action=CleanupAction(
                    kind="remote branch",
                    status="planned",
                    body=f"delete {bookmark}@origin",
                ),
                expected_remote_target=expected_target,
            ),
            review_identity=_review_identity(bookmark=bookmark),
        )

    recorded_actions: list[CleanupAction] = []
    _apply_stale_cleanup_mutation_plans(
        mutation_plans=(
            mutation_plan("review/feature-1", "commit-1"),
            mutation_plan("review/feature-2", "commit-2"),
        ),
        prepared_cleanup=prepared_cleanup,
        record_action=recorded_actions.append,
    )

    assert calls == [
        (
            "delete_remote_bookmarks",
            (
                "origin",
                (("review/feature-1", "commit-1"), ("review/feature-2", "commit-2")),
                False,
            ),
        ),
        ("forget_bookmarks", ("review/feature-1", "review/feature-2")),
        ("fetch_remote", ("origin", None)),
    ]
    assert all(action.status == "applied" for action in recorded_actions)


def test_plan_local_bookmark_cleanup_forgets_safe_review_bookmark() -> None:
    plan = cleanup_module._plan_local_bookmark_cleanup(
        cleanup_user_bookmarks=False,
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        prefix="bosullivan",
        review_identity=_review_identity(
            bookmark="bosullivan/feature-aaaaaaaa",
        ),
        stale_reason="local change is no longer reviewable",
        submitted_baseline=SubmittedBaseline(commit_id="commit-1"),
    )

    assert plan is not None
    assert plan.kind == "local bookmark"
    assert plan.status == "planned"
    assert "forget bosullivan/feature-aaaaaaaa" in plan.message
    assert "no longer reviewable" in plan.message
