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
)
from jj_stack.commands.submit.inputs import preflight_private_commits
from jj_stack.commands.submit.models import (
    PreparedSubmitRevision,
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
        head_ref="jj-stack/feature-abcdefgh",
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
    branch = "jj-stack/feature-abcdefgh"

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
    branch = "jj-stack/older-title-abcdefgh"

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
    identity = make_review_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)

    with pytest.raises(CliError, match="GitHub no longer reports a PR"):
        _ensure_pull_request_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pull_request=None,
            expected_remote_target="commit-17",
            repository_key=("octo-org", "stacked-review"),
            review_identity=identity,
            submitted_baseline=SubmittedBaseline(commit_id="commit-17"),
        )


def test_pull_request_link_rejects_a_saved_review_from_another_repository() -> None:
    identity = make_review_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)

    with pytest.raises(CliError, match="belongs to a different GitHub repository"):
        _ensure_pull_request_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pull_request=None,
            expected_remote_target="commit-17",
            repository_key=("octo-org", "other-repo"),
            review_identity=identity,
            submitted_baseline=SubmittedBaseline(commit_id="commit-17"),
        )


def test_pull_request_link_rejects_remote_and_pr_head_mismatch() -> None:
    identity = make_review_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)
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
            repository_key=("octo-org", "stacked-review"),
            review_identity=identity,
            submitted_baseline=SubmittedBaseline(commit_id="remote-commit"),
        )


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
            head_label="octo-org:jj-stack/foo",
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
                discovered_pull_requests={"jj-stack/foo": _github_pull_request(number=2)},
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
    branch: str = "jj-stack/foo",
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
