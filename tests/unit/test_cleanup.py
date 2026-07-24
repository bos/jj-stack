from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

import jj_stack.commands.cleanup.command as cleanup_module
import jj_stack.commands.cleanup.stale as stale_module
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._close_actions import plan_review_cleanup
from jj_stack.commands.cleanup.shared import PreparedCleanup
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjClient, ReviewRefUpdate
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubBranchRef, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.observation import (
    RepositoryObservation,
    ReviewObservation,
    duplicate_review_claim_change_ids,
)
from jj_stack.state.store import ReviewStateStore
from tests.support.revision_helpers import make_revision

CHANGE_ID = "aaaaaaaaabcdefgh"
BRANCH = "review/feature-aaaaaaaa"
_BASELINE = SubmittedBaseline(commit_id="saved-remote")
_REMOTE_URL = "git@github.com:octo-org/stacked-review.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)
_REPOSITORY = GithubRepoAddress(
    host="github.com",
    owner="octo-org",
    repo="stacked-review",
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
        lambda **_kwargs: (SimpleNamespace(revisions=(live_revision,)),),
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


def test_cleanup_authorizes_only_the_exact_closed_review_branch_and_lease() -> None:
    pull_request, update, blocker = plan_review_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        change_id=CHANGE_ID,
        observation=_observation(),
        review_identity=_identity(),
        submitted_baseline=_BASELINE,
    )

    assert pull_request is not None
    assert pull_request.number == 1
    assert update == ReviewRefUpdate(
        branch=BRANCH,
        expected_target=_BASELINE.commit_id,
        desired_target=None,
    )
    assert blocker is None


def test_cleanup_blocks_when_the_exact_remote_branch_drifted() -> None:
    _pull_request, update, blocker = plan_review_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        change_id=CHANGE_ID,
        observation=_observation(remote_target="external-commit"),
        review_identity=_identity(),
        submitted_baseline=_BASELINE,
    )

    assert update is None
    assert blocker is not None
    assert blocker.kind == "remote branch"
    assert "different revision" in blocker.message


def test_cleanup_preserves_branch_used_by_an_open_dependent_pull_request() -> None:
    dependent = GithubPullRequest(
        base=GithubBranchRef(ref=BRANCH),
        head=GithubBranchRef(ref="review/dependent-bbbbbbbb"),
        html_url="https://github.com/octo-org/stacked-review/pull/2",
        number=2,
        state="open",
        title="dependent",
    )

    _pull_request, update, blocker = plan_review_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        change_id=CHANGE_ID,
        observation=_observation(open_dependents=(dependent,)),
        review_identity=_identity(),
        submitted_baseline=_BASELINE,
    )

    assert update is None
    assert blocker is not None
    assert blocker.kind == "remote branch"
    assert "open PR #2 still uses" in blocker.message


@pytest.mark.parametrize("complete", (False, True))
def test_cleanup_loads_remote_context_only_for_complete_tracking_pairs(
    complete: bool,
) -> None:
    state = ReviewState(
        review_identities={CHANGE_ID: _identity()},
        submitted_baselines=({CHANGE_ID: _BASELINE} if complete else {}),
    )
    prepared = PreparedCleanup(
        context=_fake_context(),
        github_target=None,
        dry_run=False,
        state=state,
    )

    assert cleanup_module._cleanup_needs_remote_context(prepared_cleanup=prepared) is complete


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
        head_ref=BRANCH,
    )


def _pull_request() -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(
            label=f"octo-org:{BRANCH}",
            ref=BRANCH,
            sha=_BASELINE.commit_id,
        ),
        html_url="https://github.com/octo-org/stacked-review/pull/1",
        number=1,
        state="closed",
        title="feature",
    )


def _observation(
    *,
    open_dependents: tuple[GithubPullRequest, ...] = (),
    remote_target: str | None = _BASELINE.commit_id,
) -> RepositoryObservation:
    identity = _identity()
    pull_request = _pull_request()
    return RepositoryObservation(
        configured_repository=_REPOSITORY,
        duplicate_claim_change_ids=frozenset(),
        fetched_trunk_commit_id=None,
        github_repository=None,
        open_pull_requests_by_base={BRANCH: open_dependents},
        remote=_REMOTE,
        remote_trunk_target=None,
        repository=_REPOSITORY,
        reviews={
            CHANGE_ID: ReviewObservation(
                baseline=_BASELINE,
                head_pull_requests=(pull_request,),
                identity=identity,
                local_revision=None,
                pull_request=pull_request,
                remote_review_target=remote_target,
            )
        },
    )
