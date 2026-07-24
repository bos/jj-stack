from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

import jj_stack.commands.cleanup.command as cleanup_module
import jj_stack.commands.cleanup.stale as stale_module
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._close_actions import (
    BookmarkCleanupRun,
    CloseAction,
    apply_bookmark_cleanup,
    plan_bookmark_cleanup,
)
from jj_stack.commands.cleanup.shared import PreparedCleanup
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.observation import duplicate_review_claim_change_ids
from jj_stack.state.store import ReviewStateStore
from tests.support.revision_helpers import make_revision

CHANGE_ID = "aaaaaaaaabcdefgh"
BOOKMARK = "review/feature-aaaaaaaa"
_REMOTE_URL = "git@github.com:octo-org/stacked-review.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)


def _fake_context(
    *,
    jj_client: JjClient | None = None,
    state_store: ReviewStateStore | None = None,
) -> CommandContext:
    return cast(
        CommandContext,
        SimpleNamespace(
            jj_client=cast(JjClient, SimpleNamespace()) if jj_client is None else jj_client,
            state_store=(
                cast(ReviewStateStore, SimpleNamespace()) if state_store is None else state_store
            ),
        ),
    )


def _identity() -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.com",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=1,
        head_owner="octo-org",
        head_ref=BOOKMARK,
    )


def test_duplicate_claim_facts_are_scoped_to_one_repository() -> None:
    identity = _identity()
    other = identity.model_copy(update={"repository_name": "another-repository"})

    assert duplicate_review_claim_change_ids({"saved": identity, "other": other}) == frozenset()
    assert duplicate_review_claim_change_ids(
        {"saved": identity, "duplicate": identity}
    ) == frozenset({"saved", "duplicate"})


def test_local_cleanup_observations_keep_current_commit_outside_supported_stacks(
    monkeypatch,
) -> None:
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

    monkeypatch.setattr(
        stale_module,
        "discover_stacks_from_revisions",
        lambda **kwargs: (SimpleNamespace(revisions=(live_revision,)),),
    )

    observations = stale_module._local_cleanup_observations(
        change_ids=("live-change", "stale-change"),
        context=_fake_context(jj_client=cast(JjClient, FakeJjClient())),
    )

    assert observations["live-change"] == stale_module.LocalCleanupObservation(
        current_commit_id="live-commit",
        stale_reason=None,
    )
    assert observations["stale-change"] == stale_module.LocalCleanupObservation(
        current_commit_id="stale-commit",
        stale_reason="local change no longer participates in a supported stack",
    )


def test_cleanup_plan_uses_current_local_commit_and_saved_remote_baseline() -> None:
    state = BookmarkState(
        name=BOOKMARK,
        local_targets=("current-local",),
        remote_targets=(RemoteBookmarkState(remote="origin", targets=("saved-remote",)),),
    )

    plan = plan_bookmark_cleanup(
        bookmark=BOOKMARK,
        bookmark_state=state,
        change_id=CHANGE_ID,
        local_commit_id="current-local",
        record_action=lambda action: None,
        remote_commit_id="saved-remote",
        remote_name="origin",
        review_identity=_identity(),
    )

    assert plan.local_forget is True
    assert plan.remote_delete is True
    assert plan.blocked is False


def test_apply_cleanup_rechecks_bookmark_before_any_mutation() -> None:
    initial_state = BookmarkState(
        name=BOOKMARK,
        local_targets=("current-local",),
        remote_targets=(RemoteBookmarkState(remote="origin", targets=("saved-remote",)),),
    )
    moved_state = initial_state.model_copy(update={"local_targets": ("moved-local",)})
    calls: list[str] = []

    class RecordingJjClient:
        def get_bookmark_state(self, bookmark):
            calls.append("get")
            return moved_state

        def list_remote_branches(self, *, remote, patterns):
            calls.append("list")
            return {BOOKMARK: "saved-remote"}

        def delete_remote_bookmarks(self, **kwargs):
            calls.append("delete")

        def forget_bookmarks(self, bookmarks):
            calls.append("forget")

    actions: list[CloseAction] = []
    plan = plan_bookmark_cleanup(
        bookmark=BOOKMARK,
        bookmark_state=initial_state,
        change_id=CHANGE_ID,
        local_commit_id="current-local",
        record_action=actions.append,
        remote_commit_id="saved-remote",
        remote_name="origin",
        review_identity=_identity(),
    )
    run = cast(
        BookmarkCleanupRun,
        SimpleNamespace(
            dry_run=False,
            jj_client=cast(JjClient, RecordingJjClient()),
        ),
    )

    current = apply_bookmark_cleanup(
        bookmark=BOOKMARK,
        change_id=CHANGE_ID,
        cleanup_plan=plan,
        local_commit_id="current-local",
        record_action=actions.append,
        remote_commit_id="saved-remote",
        remote_name="origin",
        review_identity=_identity(),
        run=run,
    )

    assert current is False
    assert calls == ["get", "list"]
    assert actions[-1].status == "blocked"


@pytest.mark.parametrize(
    ("initial_remote_target", "live_remote_target"),
    (
        (None, "saved-remote"),
        ("saved-remote", "moved-remote"),
    ),
)
def test_apply_cleanup_blocks_when_exact_remote_ref_changes_before_mutation(
    initial_remote_target: str | None,
    live_remote_target: str,
) -> None:
    initial_remote_states = (
        ()
        if initial_remote_target is None
        else (
            RemoteBookmarkState(
                remote="origin",
                targets=(initial_remote_target,),
            ),
        )
    )
    initial_state = BookmarkState(
        name=BOOKMARK,
        local_targets=("current-local",),
        remote_targets=initial_remote_states,
    )
    calls: list[str] = []

    class RecordingJjClient:
        def get_bookmark_state(self, bookmark):
            calls.append("get")
            return initial_state

        def list_remote_branches(self, *, remote, patterns):
            calls.append("list")
            return {BOOKMARK: live_remote_target}

        def delete_remote_bookmarks(self, **kwargs):
            calls.append("delete")

        def forget_bookmarks(self, bookmarks):
            calls.append("forget")

    actions: list[CloseAction] = []
    plan = plan_bookmark_cleanup(
        bookmark=BOOKMARK,
        bookmark_state=initial_state,
        change_id=CHANGE_ID,
        local_commit_id="current-local",
        record_action=actions.append,
        remote_commit_id="saved-remote",
        remote_name="origin",
        review_identity=_identity(),
    )

    current = apply_bookmark_cleanup(
        bookmark=BOOKMARK,
        change_id=CHANGE_ID,
        cleanup_plan=plan,
        local_commit_id="current-local",
        record_action=actions.append,
        remote_commit_id="saved-remote",
        remote_name="origin",
        review_identity=_identity(),
        run=cast(
            BookmarkCleanupRun,
            SimpleNamespace(
                dry_run=False,
                jj_client=cast(JjClient, RecordingJjClient()),
            ),
        ),
    )

    assert current is False
    assert calls == ["get", "list"]
    assert actions[-1].status == "blocked"


def test_cleanup_remote_context_uses_only_complete_tracking_pairs() -> None:
    state = ReviewState(review_identities={CHANGE_ID: _identity()})
    prepared = PreparedCleanup(
        context=_fake_context(),
        bookmark_states={},
        github_target=GithubTarget(
            remote=_REMOTE,
            repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
        ),
        dry_run=False,
        state=state,
    )

    assert cleanup_module._cleanup_needs_remote_context(prepared_cleanup=prepared) is False


def test_refresh_review_branches_ignores_other_repository_without_fetching() -> None:
    class NoFetchJjClient:
        def fetch_remote(self, **kwargs):
            raise AssertionError("repository-mismatched branches must not be fetched")

    identity = _identity().model_copy(update={"repository_name": "another-repository"})
    state = ReviewState(
        review_identities={CHANGE_ID: identity},
        submitted_baselines={CHANGE_ID: SubmittedBaseline(commit_id="saved-remote")},
    )
    prepared = PreparedCleanup(
        context=_fake_context(jj_client=cast(JjClient, NoFetchJjClient())),
        bookmark_states={},
        github_target=GithubTarget(
            remote=_REMOTE,
            repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
        ),
        dry_run=False,
        state=state,
    )

    assert cleanup_module._refresh_review_branches(prepared_cleanup=prepared) is prepared
