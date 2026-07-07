from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.land.command import _stack_not_on_trunk_error
from jj_stack.commands.land.execute import (
    _finalize_landed_pull_request,
    _updated_landed_change,
    ensure_trunk_branch_matches_selected_trunk,
)
from jj_stack.commands.land.models import LandRevision
from jj_stack.commands.land.plan import (
    _collect_landable_prefix,
    _plan_review_bookmark_cleanup,
)
from jj_stack.config import RepoConfig
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import CachedChange, LinkState
from jj_stack.review.status import (
    PreparedStatus,
    PullRequestLookup,
    PullRequestLookupState,
    ReviewStatusRevision,
    StatusResult,
)
from jj_stack.ui import plain_text


class _FakeJjClient:
    def __init__(self, diffs: dict[str, str] | None = None) -> None:
        self.diffs = diffs or {}
        self.diff_calls: list[str] = []

    def get_commit_diff(self, commit_id: str) -> str:
        self.diff_calls.append(commit_id)
        return self.diffs[commit_id]


def _jj_client(diffs: dict[str, str] | None = None) -> JjClient:
    return cast(JjClient, _FakeJjClient(diffs))


def _fake_context() -> CommandContext:
    return cast(
        CommandContext,
        SimpleNamespace(config=RepoConfig()),
    )


def _land_boundary_message(
    *,
    bypass_readiness: bool,
    client: JjClient,
    prepared_revision,
    revision,
):
    _planned_revisions, boundary_action = _collect_landable_prefix(
        bypass_readiness=bypass_readiness,
        client=client,
        path_revisions=((prepared_revision, revision),),
    )
    if boundary_action is None:
        return None
    return boundary_action.body


def test_landable_prefix_marks_diff_equivalent_revision_for_resubmit() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        remote_target="other-tip",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
    )
    client = _FakeJjClient({"commit-1": "same", "other-tip": "same"})

    planned_revisions, boundary_action = _collect_landable_prefix(
        bypass_readiness=False,
        client=cast(JjClient, client),
        path_revisions=((prepared_revision, revision),),
    )

    assert boundary_action is None
    assert client.diff_calls == ["commit-1", "other-tip"]
    assert len(planned_revisions) == 1
    assert planned_revisions[0].needs_resubmit is True


def test_land_boundary_message_allows_ready_revision_without_remote_state() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
        with_remote_state=False,
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        client=_jj_client(),
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is None


def test_land_boundary_message_blocks_content_divergent_revision() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        remote_target="old-commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        client=_jj_client({"commit-1": "local", "old-commit-1": "remote"}),
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is not None
    assert "differs from what reviewers approved" in plain_text(message)


def test_land_boundary_message_prefers_unlinked_state_over_content_divergence() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        link_state="unlinked",
        remote_target="old-commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        client=_jj_client(),
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is not None
    rendered = plain_text(message)
    assert "unlinked from review tracking" in rendered
    assert "differs from what reviewers approved" not in rendered


def test_land_boundary_message_prefers_missing_pr_over_content_divergence() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        remote_target="old-commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="missing",
        subject="feature 1",
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        client=_jj_client(),
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is not None
    rendered = plain_text(message)
    assert "GitHub no longer reports a pull request" in rendered
    assert "differs from what reviewers approved" not in rendered


def test_stack_not_on_trunk_error_recommends_rebase_when_no_changes_have_landed() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"), selected_revset="@-")
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-2",
                    commit_id="commit-2",
                    pull_request=_pull_request(number=2),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 2",
                ),
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 1",
                ),
            ),
            selected_revset="@-",
        ),
    )

    error = _stack_not_on_trunk_error(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    assert plain_text(error.message) == "Selected stack is not based on the current trunk()."
    assert error.hint is not None
    rendered_hint = plain_text(error.hint)
    assert "jj rebase -s change-1 -d 'trunk()'" in rendered_hint
    assert "cleanup --rebase" not in rendered_hint


def test_stack_not_on_trunk_error_recommends_cleanup_when_stack_has_landed_change() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"), selected_revset="@-")
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-2",
                    commit_id="commit-2",
                    pull_request=_pull_request(number=2),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 2",
                ),
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1).model_copy(
                        update={"state": "merged", "merged_at": "2026-03-22T12:00:00Z"}
                    ),
                    pull_request_state="closed",
                    subject="feature 1",
                ),
            ),
            selected_revset="@-",
        ),
    )

    error = _stack_not_on_trunk_error(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    assert plain_text(error.message) == "Selected stack is not based on the current trunk()."
    assert error.hint is not None
    rendered_hint = plain_text(error.hint)
    assert "cleanup --rebase @-" in rendered_hint
    assert "jj rebase -s" not in rendered_hint


def test_landed_revision_updates_cached_change_after_merge() -> None:
    updated = _updated_landed_change(
        bookmark="review/feature-1-aaaaaaaa",
        bookmark_managed=True,
        cached_change=CachedChange(
            bookmark="review/feature-1-aaaaaaaa",
            last_submitted_commit_id="old-commit",
            pr_number=1,
            pr_review_decision="approved",
            pr_state="open",
            pr_url="https://github.test/octo-org/stacked-review/pull/1",
            navigation_comment_id=99,
            overview_comment_id=100,
        ),
        commit_id="new-commit",
        parent_change_id=None,
        pull_request=GithubPullRequest(
            base=GithubBranchRef(ref="main"),
            head=GithubBranchRef(ref="review/feature-1-aaaaaaaa"),
            html_url="https://github.test/octo-org/stacked-review/pull/1",
            merged_at="2026-03-22T12:00:00Z",
            number=1,
            state="closed",
            title="feature 1",
        ),
        stack_head_change_id="feature1head000000",
    )

    assert updated.last_submitted_commit_id == "new-commit"
    assert updated.last_submitted_parent_change_id is None
    assert updated.last_submitted_stack_head_change_id == "feature1head000000"
    assert updated.pr_review_decision is None
    assert updated.pr_state == "merged"
    assert updated.navigation_comment_id is None
    assert updated.overview_comment_id is None


def test_finalize_landed_pull_request_treats_close_422_as_already_merged() -> None:
    class CloseRaceGithubClient:
        def __init__(self) -> None:
            self.close_calls = 0
            self.get_calls = 0

        async def get_pull_request(self, *, pull_number: int) -> GithubPullRequest:
            self.get_calls += 1
            if self.get_calls == 1:
                return _pull_request(number=pull_number)
            return _pull_request(number=pull_number, state="merged")

        async def close_pull_request(self, *, pull_number: int) -> None:
            self.close_calls += 1
            raise GithubClientError(
                'GitHub request failed: 422 {"message":"Validation Failed"}',
                status_code=422,
            )

    github_client = CloseRaceGithubClient()

    pull_request = asyncio.run(
        _finalize_landed_pull_request(
            cached_change=None,
            github_client=cast(GithubClient, github_client),
            landed_revision=LandRevision(
                bookmark="review/feature-1-aaaaaaaa",
                bookmark_managed=True,
                change_id="change-1",
                commit_id="commit-1",
                needs_resubmit=False,
                pull_request_number=1,
                subject="feature 1",
            ),
            trunk_branch="main",
        )
    )

    assert pull_request.state == "merged"
    assert github_client.close_calls == 1
    assert github_client.get_calls == 2


def test_finalize_landed_pull_request_does_not_recover_close_422_as_closed() -> None:
    class CloseRaceGithubClient:
        def __init__(self) -> None:
            self.close_calls = 0
            self.get_calls = 0

        async def get_pull_request(self, *, pull_number: int) -> GithubPullRequest:
            self.get_calls += 1
            if self.get_calls == 1:
                return _pull_request(number=pull_number)
            return _pull_request(number=pull_number, state="closed")

        async def close_pull_request(self, *, pull_number: int) -> None:
            self.close_calls += 1
            raise GithubClientError(
                'GitHub request failed: 422 {"message":"Validation Failed"}',
                status_code=422,
            )

    github_client = CloseRaceGithubClient()

    with pytest.raises(CliError, match="Could not close PR #1 after landing"):
        asyncio.run(
            _finalize_landed_pull_request(
                cached_change=None,
                github_client=cast(GithubClient, github_client),
                landed_revision=LandRevision(
                    bookmark="review/feature-1-aaaaaaaa",
                    bookmark_managed=True,
                    change_id="change-1",
                    commit_id="commit-1",
                    needs_resubmit=False,
                    pull_request_number=1,
                    subject="feature 1",
                ),
                trunk_branch="main",
            )
        )

    assert github_client.close_calls == 1
    assert github_client.get_calls == 2


def test_plan_review_bookmark_cleanup_forgets_owned_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="bosullivan/feature-aaaaaaaa",
        bookmark_managed=True,
        cleanup_user_bookmarks=False,
        prefix="bosullivan",
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.can_forget is True
    assert plan.action.message == "forget bosullivan/feature-aaaaaaaa"
    assert plan.action.status == "planned"


def test_plan_review_bookmark_cleanup_skips_external_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_managed=False,
        cleanup_user_bookmarks=False,
        prefix="review",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is None


def test_plan_review_bookmark_cleanup_forgets_external_bookmark_when_configured() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="potato/feature-aaaaaaaa",
        bookmark_managed=False,
        cleanup_user_bookmarks=True,
        prefix="review",
        bookmark_state=BookmarkState(
            name="potato/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.can_forget is True
    assert plan.action.status == "planned"


def test_plan_review_bookmark_cleanup_blocks_conflicted_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_managed=True,
        cleanup_user_bookmarks=False,
        prefix="review",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1", "commit-2"),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.can_forget is False
    assert "is conflicted" in plan.action.message
    assert plan.action.status == "blocked"


def test_plan_review_bookmark_cleanup_blocks_moved_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_managed=True,
        cleanup_user_bookmarks=False,
        prefix="review",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-2",),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.can_forget is False
    assert "points to a different revision" in plan.action.message
    assert plan.action.status == "blocked"


def test_ensure_trunk_branch_matches_selected_trunk_rejects_missing_remote_bookmark() -> None:
    client = _BookmarkClientStub(BookmarkState(name="main", local_targets=("commit-1",)))

    with pytest.raises(CliError, match="Remote trunk bookmark main@origin is not available"):
        ensure_trunk_branch_matches_selected_trunk(
            client=client,
            remote_name="origin",
            trunk_branch="main",
            trunk_commit_id="commit-1",
        )


@pytest.mark.parametrize(
    ("bookmark_state", "message"),
    [
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-1", "commit-2"),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            "Local trunk bookmark main is conflicted",
            id="conflicted-local",
        ),
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-2",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            "Local bookmark main points to a different revision",
            id="moved-local",
        ),
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-1",),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("commit-1", "commit-2")),
                ),
            ),
            "Remote trunk bookmark main@origin is conflicted",
            id="conflicted-remote",
        ),
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-1",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-2",)),),
            ),
            "Remote trunk bookmark main@origin moved",
            id="moved-remote",
        ),
    ],
)
def test_ensure_trunk_branch_matches_selected_trunk_rejects_unsafe_bookmarks(
    bookmark_state: BookmarkState,
    message: str,
) -> None:
    client = _BookmarkClientStub(bookmark_state)

    with pytest.raises(CliError, match=message):
        ensure_trunk_branch_matches_selected_trunk(
            client=client,
            remote_name="origin",
            trunk_branch="main",
            trunk_commit_id="commit-1",
        )


def _status_revision(
    *,
    change_id: str,
    commit_id: str,
    remote_target: str | None = None,
    with_remote_state: bool = True,
    pull_request: GithubPullRequest,
    pull_request_state: PullRequestLookupState,
    review_decision: str | None = None,
    review_decision_error: str | None = None,
    subject: str,
    link_state: LinkState = "active",
) -> ReviewStatusRevision:
    return ReviewStatusRevision(
        bookmark=f"review/{change_id}",
        bookmark_source="generated",
        cached_change=None,
        change_id=change_id,
        commit_id=commit_id,
        link_state=link_state,
        local_divergent=False,
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=pull_request,
            review_decision=review_decision,
            review_decision_error=review_decision_error,
            state=pull_request_state,
        ),
        remote_state=(
            RemoteBookmarkState(
                remote="origin",
                targets=((remote_target,) if remote_target is not None else (commit_id,)),
            )
            if with_remote_state
            else None
        ),
        managed_comments_lookup=None,
        subject=subject,
    )


def _pull_request(
    *,
    number: int,
    state: str = "open",
    draft: bool = False,
) -> GithubPullRequest:
    merged_at = "2026-03-22T12:00:00Z" if state == "merged" else None
    pr_state = "closed" if state == "merged" else state
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        draft=draft,
        head=GithubBranchRef(ref=f"review/{number}"),
        html_url=f"https://github.test/octo-org/stacked-review/pull/{number}",
        merged_at=merged_at,
        number=number,
        state=pr_state,
        title=f"feature {number}",
    )


def _prepared_status(
    change_ids: tuple[str, ...],
    *,
    commit_ids: tuple[str, ...] | None = None,
    selected_revset: str = "@-",
) -> PreparedStatus:
    resolved_commit_ids = commit_ids or tuple(
        f"commit-{index + 1}" for index, _change_id in enumerate(change_ids)
    )
    status_revisions = tuple(
        SimpleNamespace(
            revision=SimpleNamespace(
                change_id=change_id,
                commit_id=commit_id,
                conflict=False,
            )
        )
        for change_id, commit_id in zip(change_ids, resolved_commit_ids, strict=True)
    )
    return cast(
        PreparedStatus,
        SimpleNamespace(
            github_inspection_count=lambda *, discover_remote_review=False: 0,
            prepared=SimpleNamespace(
                stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                status_revisions=status_revisions,
            ),
            selected_revset=selected_revset,
        ),
    )


class _BookmarkClientStub:
    def __init__(self, bookmark_state: BookmarkState) -> None:
        self._bookmark_state = bookmark_state

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        assert bookmark == self._bookmark_state.name
        return self._bookmark_state
