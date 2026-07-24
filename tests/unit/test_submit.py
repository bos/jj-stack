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
from jj_stack.commands.submit.command import (
    _resolve_submit_options,
    _validate_restart_recovery_candidates,
)
from jj_stack.commands.submit.inputs import preflight_private_commits
from jj_stack.commands.submit.models import (
    GeneratedDescription,
    PendingPullRequestSync,
    PreparedSubmitRevision,
    SubmitMutationRun,
    SubmitOptions,
)
from jj_stack.commands.submit.pull_requests import (
    _ensure_pull_request_link_is_consistent,
    _reviewers_to_re_request,
    _select_discovered_pull_request,
)
from jj_stack.commands.submit.revisions import prepare_submit_revisions
from jj_stack.config import AppConfig
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import (
    GithubBranchRef,
    GithubPullRequest,
    GithubPullRequestReview,
    GithubPullRequestReviewUser,
)
from jj_stack.models.review_state import ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.branches import ResolvedReviewBranch
from jj_stack.review.restart import RestartedReview
from jj_stack.state.store import ReviewStateStore
from tests.support.review_state import make_review_identity
from tests.support.revision_helpers import make_revision

_REMOTE_URL = "https://github.test/octo-org/repo.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)


def test_prepare_submit_revisions_rejects_saved_remote_branch_drift() -> None:
    revision = make_revision(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    identity = make_review_identity(
        head_ref="review/feature-abcdefgh",
        pr_number=17,
    )

    with pytest.raises(CliError, match="unexpected commit"):
        prepare_submit_revisions(
            branch_resolutions=(
                ResolvedReviewBranch(
                    branch=identity.head_ref,
                    change_id=revision.change_id,
                ),
            ),
            remote_targets={identity.head_ref: "external-commit"},
            remote=_REMOTE,
            stack=_local_stack(revision),
            state=ReviewState(
                review_identities={revision.change_id: identity},
                submitted_baselines={
                    revision.change_id: SubmittedBaseline(commit_id="submitted-commit")
                },
            ),
        )


def test_prepare_submit_revisions_rejects_unclaimed_existing_branch() -> None:
    revision = make_revision(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    branch = "review/feature-abcdefgh"

    with pytest.raises(CliError, match="already exists"):
        prepare_submit_revisions(
            branch_resolutions=(
                ResolvedReviewBranch(
                    branch=branch,
                    change_id=revision.change_id,
                ),
            ),
            remote_targets={branch: "another-commit"},
            remote=_REMOTE,
            stack=_local_stack(revision),
            state=ReviewState(),
        )


def test_prepare_submit_revisions_requires_recovered_branch_lease_to_stay_exact() -> None:
    revision = make_revision(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    branch = "review/older-title-abcdefgh"

    with pytest.raises(CliError, match="changed during submission"):
        prepare_submit_revisions(
            branch_resolutions=(
                ResolvedReviewBranch(
                    branch=branch,
                    change_id=revision.change_id,
                    recovered_target="interrupted-commit",
                ),
            ),
            remote_targets={branch: "external-commit"},
            remote=_REMOTE,
            stack=_local_stack(revision),
            state=ReviewState(),
        )


def test_pull_request_link_rejects_missing_discovered_pull_request() -> None:
    identity = make_review_identity(head_ref="review/foo-abcdefgh", pr_number=17)

    with pytest.raises(CliError, match="GitHub no longer reports a PR"):
        _ensure_pull_request_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pull_request=None,
            expected_remote_target="commit-17",
            review_identity=identity,
            submitted_baseline=SubmittedBaseline(commit_id="commit-17"),
        )


def test_pull_request_link_rejects_remote_and_pr_head_mismatch() -> None:
    identity = make_review_identity(head_ref="review/foo-abcdefgh", pr_number=17)
    pull_request = _github_pull_request(
        number=17,
        branch=identity.head_ref,
        head_sha="github-commit",
    )

    with pytest.raises(CliError, match="remote branch no longer identify the same commit"):
        _ensure_pull_request_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pull_request=pull_request,
            expected_remote_target="remote-commit",
            review_identity=identity,
            submitted_baseline=SubmittedBaseline(commit_id="remote-commit"),
        )


def test_restart_recovery_rejects_a_changed_remote_branch() -> None:
    prepared = _prepared_revision(
        branch="review/feature-fresh-pr17-abcdefgh",
        change_id="abcdefghijk",
        commit_id="new-commit",
    )
    pull_request = _github_pull_request(
        number=18,
        branch=prepared.branch,
        head_sha=prepared.revision.commit_id,
    )

    with pytest.raises(CliError, match="replacement branch"):
        _validate_restart_recovery_candidates(
            head_owner="octo-org",
            pending_syncs=(
                PendingPullRequestSync(
                    base_branch="main",
                    discovered_pull_request=pull_request,
                    generated_description=GeneratedDescription(title="feature", body=""),
                    parent_change_id=None,
                    prepared=prepared,
                    stack_head_change_id=prepared.revision.change_id,
                ),
            ),
            remote_targets={prepared.branch: "external-commit"},
            restarted_change_ids=frozenset({prepared.revision.change_id}),
        )


def test_restart_submission_replaces_the_exact_saved_pair_only_after_staging() -> None:
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
        change_id=change_id,
        identity=old_identity,
        new_branch=new_identity.head_ref,
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

    run.commit_restart_submissions()

    assert calls == [
        {
            "expected": {change_id: (old_identity, old_baseline)},
            "replacements": {change_id: (new_identity, new_baseline)},
        }
    ]
    assert run.state == replacement_state


def test_preflight_private_commits_rejects_blocked_revision() -> None:
    private = make_revision(
        commit_id="head",
        change_id="head-change",
        description="private thing\n",
    )

    class PrivateCommitClient:
        def find_private_commits(
            self,
            revisions: tuple[LocalRevision, ...],
        ) -> tuple[LocalRevision, ...]:
            del revisions
            return (private,)

    with pytest.raises(CliError, match="git.private-commits"):
        preflight_private_commits(PrivateCommitClient(), (private,))


@pytest.mark.parametrize(
    ("states", "message"),
    (
        (("open", "open"), "multiple pull requests"),
        (("closed",), "in state closed"),
    ),
)
def test_discovered_pull_request_must_be_unique_and_open(
    states: tuple[str, ...],
    message: str,
) -> None:
    pull_requests = tuple(
        _github_pull_request(number=number, state=state)
        for number, state in enumerate(states, start=1)
    )
    with pytest.raises(CliError, match=message):
        _select_discovered_pull_request(
            head_label="octo-org:review/foo",
            pull_requests=pull_requests,
        )


def test_reviewers_to_re_request_uses_latest_actionable_state_per_reviewer() -> None:
    reviews = _reviews(
        (1, "alice", "APPROVED"),
        (2, "alice", "DISMISSED"),
        (3, "erin", "CHANGES_REQUESTED"),
        (4, "erin", "APPROVED"),
        (5, "dave", "COMMENTED"),
    )

    assert _reviewers_to_re_request(reviews) == ["erin"]


@pytest.mark.parametrize("refetched", (None, "closed"))
def test_submit_detects_pull_request_that_is_no_longer_open(
    refetched: str | None,
) -> None:
    client = _RefetchPullRequestsClient(
        refetched={
            2: (None if refetched is None else _github_pull_request(number=2, state=refetched))
        },
    )

    with pytest.raises(CliError):
        asyncio.run(
            verify_no_unexpected_pull_request_closures(
                discovered_pull_requests={"review/foo": _github_pull_request(number=2)},
                github_client=cast(GithubClient, client),
            )
        )


def test_resolve_submit_options_prefers_cli_values_over_config() -> None:
    context = cast(
        CommandContext,
        SimpleNamespace(
            config=AppConfig(
                labels=["config-label"],
                reviewers=["config-user"],
                team_reviewers=["config-team"],
            )
        ),
    )

    resolved = _resolve_submit_options(
        context=context,
        options=replace(
            _submit_options(),
            labels=["cli-label"],
            reviewers=["cli-user"],
        ),
    )

    assert resolved.labels == ["cli-label"]
    assert resolved.reviewers == ["cli-user"]
    assert resolved.team_reviewers == ["config-team"]


def _submit_options() -> SubmitOptions:
    return SubmitOptions(
        descriptions=(),
        describe_with=None,
        draft_mode="default",
        dry_run=False,
        edit=False,
        existing_only=False,
        labels=None,
        re_request=False,
        restart=False,
        reviewers=None,
        revset="@",
        team_reviewers=None,
    )


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
    *,
    branch: str,
    change_id: str,
    commit_id: str,
) -> PreparedSubmitRevision:
    return PreparedSubmitRevision(
        branch=branch,
        expected_remote_target="old-commit",
        remote_action="pushed",
        revision=make_revision(
            commit_id=commit_id,
            change_id=change_id,
            description=f"{branch}\n",
        ),
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


class _RefetchPullRequestsClient:
    def __init__(self, *, refetched: dict[int, GithubPullRequest | None]) -> None:
        self._refetched = refetched

    async def get_pull_requests_by_numbers(
        self,
        *,
        pull_numbers,
    ) -> dict[int, GithubPullRequest | None]:
        return {number: self._refetched.get(number) for number in pull_numbers}


def _github_pull_request(
    number: int,
    *,
    branch: str = "review/foo",
    head_sha: str = "head-commit",
    state: str = "open",
) -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        body="",
        head=GithubBranchRef(
            label=f"octo-org:{branch}",
            ref=branch,
            sha=head_sha,
        ),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        number=number,
        state=state,
        title="feature",
    )
