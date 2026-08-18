from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import jj_stack.commands.cleanup.stale as stale_module
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._cleanup_actions import plan_pr_cleanup
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjClient, PRRefUpdate
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubBranchRef, GithubPR
from jj_stack.models.tracking import (
    PRIdentity,
    SubmittedBaseline,
    TrackedPR,
    TrackingState,
)
from jj_stack.stack.pr_facts import (
    PRFacts,
    RepoFacts,
    duplicate_pr_claim_change_ids,
)
from jj_stack.state.store import TrackingStore
from jj_stack.ui import plain_text
from tests.support.change_helpers import make_change

CHANGE_ID = "aaaaaaaaabcdefgh"
BRANCH = "jj-stack/feature-aaaaaaaa"
_BASELINE = SubmittedBaseline(commit_id="saved-remote")
_REMOTE_URL = "git@github.com:octo-org/stacked-prs.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)
_REPO = GithubRepoAddress(
    owner="octo-org",
    repo="stacked-prs",
)


def test_duplicate_claim_facts_are_scoped_to_one_repo() -> None:
    identity = _identity()
    other = identity.model_copy(update={"repo_name": "another-repository"})

    assert duplicate_pr_claim_change_ids({"saved": identity, "other": other}) == frozenset()
    assert duplicate_pr_claim_change_ids({"saved": identity, "duplicate": identity}) == frozenset(
        {"saved", "duplicate"}
    )


def test_local_cleanup_observations_flag_changes_outside_current_stacks(
    monkeypatch,
) -> None:
    live_change = make_change(
        change_id="live-change",
        commit_id="live-commit",
        description="live\n",
    )
    stale_change = make_change(
        change_id="stale-change",
        commit_id="stale-commit",
        description="stale\n",
    )

    class FakeJjClient:
        def query_commits_by_change_ids(self, change_ids):
            assert change_ids == ("live-change", "stale-change")
            return {
                "live-change": (live_change,),
                "stale-change": (stale_change,),
            }

    monkeypatch.setattr(
        stale_module,
        "observe_repo_paths",
        lambda **_kwargs: SimpleNamespace(
            paths=(SimpleNamespace(stack=SimpleNamespace(changes=(live_change,))),)
        ),
    )

    observations = stale_module.local_cleanup_observations(
        change_ids=("live-change", "stale-change"),
        context=_fake_context(jj_client=cast(JjClient, FakeJjClient())),
    )

    assert observations["live-change"] == stale_module.LocalCleanupObservation(
        has_mutable_copy=True,
        stale_reason=None,
    )
    stale_observation = observations["stale-change"]
    assert stale_observation.has_mutable_copy
    assert stale_observation.stale_reason is not None


def test_cleanup_accepts_only_the_exact_closed_pr_branch_and_lease() -> None:
    pr, update, blocker = plan_pr_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        candidate=_candidate(),
        observation=_observation(),
    )

    assert pr is not None
    assert pr.number == 1
    assert update == PRRefUpdate(
        branch=BRANCH,
        expected_target=_BASELINE.commit_id,
        desired_target=None,
    )
    assert blocker is None


def test_cleanup_blocks_when_the_exact_remote_branch_drifted() -> None:
    _pr, update, blocker = plan_pr_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        candidate=_candidate(),
        observation=_observation(remote_target="external-commit"),
    )

    assert update is None
    assert blocker is not None
    assert blocker.kind == "remote branch"
    assert "different commit" in plain_text(blocker.body)


def test_cleanup_preserves_a_head_branch_shared_by_another_open_pr() -> None:
    competing_pr = _pr().model_copy(
        update={
            "base": GithubBranchRef(ref="release"),
            "number": 2,
            "state": "open",
        }
    )

    _pr_result, update, blocker = plan_pr_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        candidate=_candidate(),
        observation=_observation(open_head_prs=(competing_pr,)),
    )

    assert update is None
    assert blocker is not None
    assert blocker.kind == "remote branch"
    assert "another open pull request" in plain_text(blocker.body)


def _fake_context(
    *,
    jj_client: JjClient | None = None,
    state_store: TrackingStore | None = None,
) -> CommandContext:
    return cast(
        CommandContext,
        SimpleNamespace(
            jj_client=cast(JjClient, SimpleNamespace()) if jj_client is None else jj_client,
            state_store=(
                cast(TrackingStore, SimpleNamespace(load=TrackingState))
                if state_store is None
                else state_store
            ),
        ),
    )


def _identity() -> PRIdentity:
    return PRIdentity(
        repo_owner="octo-org",
        repo_name="stacked-prs",
        pr_number=1,
        head_owner="octo-org",
        head_ref=BRANCH,
    )


def _candidate() -> TrackedPR:
    return TrackedPR(
        change_id=CHANGE_ID,
        pr_identity=_identity(),
        submitted_baseline=_BASELINE,
    )


def _pr() -> GithubPR:
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(
            label=f"octo-org:{BRANCH}",
            ref=BRANCH,
            sha=_BASELINE.commit_id,
        ),
        html_url="https://github.com/octo-org/stacked-prs/pull/1",
        number=1,
        state="closed",
        title="feature",
    )


def _observation(
    *,
    open_head_prs: tuple[GithubPR, ...] = (),
    remote_target: str | None = _BASELINE.commit_id,
) -> RepoFacts:
    identity = _identity()
    pr = _pr()
    return RepoFacts(
        configured_repo=_REPO,
        github_repo=None,
        open_prs_by_base={BRANCH: ()},
        remote=_REMOTE,
        repo=_REPO,
        prs={
            CHANGE_ID: PRFacts(
                baseline=_BASELINE,
                open_head_prs=open_head_prs,
                identity=identity,
                local_commits=(),
                pr=pr,
                remote_pr_branch_target=remote_target,
            )
        },
    )
