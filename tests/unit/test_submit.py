from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.submit.command import (
    _pull_request_sync_plans,
)
from jj_stack.commands.submit.inputs import preflight_private_commits
from jj_stack.commands.submit.models import (
    GeneratedDescription,
    PreparedSubmitRevision,
    SubmitOptions,
)
from jj_stack.commands.submit.overview_comments import sync_stack_overview_comments
from jj_stack.commands.submit.pull_requests import (
    _select_discovered_pull_request,
    ensure_pull_request_link_is_consistent,
)
from jj_stack.commands.submit.revisions import prepare_submit_revisions
from jj_stack.config import AppConfig
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import (
    GithubBranchRef,
    GithubIssueComment,
    GithubPullRequest,
)
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
    TrackedReview,
)
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.branches import ResolvedReviewBranch
from tests.support.review_state import make_review_identity
from tests.support.revision_helpers import make_revision

_REMOTE_URL = "https://github.test/octo-org/repo.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)


def test_overview_comment_sync_batches_comment_reads() -> None:
    class CommentClientStub(GithubClient):
        def __init__(self) -> None:
            self.comment_batches: list[tuple[int, ...]] = []

        async def get_issue_comments_by_pull_request_numbers(
            self,
            *,
            pull_numbers: Sequence[int],
        ) -> dict[int, tuple[GithubIssueComment, ...]]:
            self.comment_batches.append(tuple(pull_numbers))
            return {number: () for number in pull_numbers}

        async def list_issue_comments(
            self,
            *,
            issue_number: int,
        ) -> tuple[GithubIssueComment, ...]:
            raise AssertionError(f"unexpected per-PR comment lookup for #{issue_number}")

    client = CommentClientStub()

    asyncio.run(
        sync_stack_overview_comments(
            concurrency=2,
            github_client=client,
            overview_bodies={1: None, 2: None},
        )
    )

    assert client.comment_batches == [(1, 2)]


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
        ensure_pull_request_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pull_request=None,
            expected_remote_target="commit-17",
            repository_key=("octo-org", "stacked-review"),
            tracked_review=_tracked_review(identity),
        )


def test_pull_request_link_rejects_a_saved_review_from_another_repository() -> None:
    identity = make_review_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)

    with pytest.raises(CliError, match="belongs to a different GitHub repository"):
        ensure_pull_request_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pull_request=None,
            expected_remote_target="commit-17",
            repository_key=("octo-org", "other-repo"),
            tracked_review=_tracked_review(identity),
        )


def test_pull_request_link_rejects_remote_and_pr_head_mismatch() -> None:
    identity = make_review_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)
    pull_request = _github_pull_request(
        number=17,
        branch=identity.head_ref,
        head_sha="github-commit",
    )

    with pytest.raises(CliError, match="remote branch no longer identify the same commit"):
        ensure_pull_request_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pull_request=pull_request,
            expected_remote_target="remote-commit",
            repository_key=("octo-org", "stacked-review"),
            tracked_review=_tracked_review(identity, commit_id="remote-commit"),
        )


def _tracked_review(
    identity: ReviewIdentity,
    *,
    commit_id: str = "commit-17",
) -> TrackedReview:
    return TrackedReview(
        change_id="abcdefghijk",
        review_identity=identity,
        submitted_baseline=SubmittedBaseline(commit_id=commit_id),
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


def test_discovered_pull_request_must_have_only_one_open_review() -> None:
    pull_requests = tuple(
        _github_pull_request(number=number, state=state)
        for number, state in enumerate(("open", "open"), start=1)
    )
    with pytest.raises(CliError, match="multiple pull requests"):
        _select_discovered_pull_request(
            head_label="octo-org:jj-stack/foo",
            pull_requests=pull_requests,
            tracked_pull_number=None,
        )


def test_pull_request_plan_prefers_cli_metadata_over_config() -> None:
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

    revision = make_revision(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    branch = "jj-stack/feature-abcdefgh"
    plans = _pull_request_sync_plans(
        bottom_base_branch="main",
        context=context,
        discovered_pull_requests={branch: _github_pull_request(number=17, branch=branch)},
        drafts={revision.change_id: False},
        generated_descriptions={
            revision.change_id: GeneratedDescription(body="", title="feature")
        },
        options=replace(
            _submit_options(),
            labels=["cli-label"],
            reviewers=["cli-user"],
        ),
        prepared_revisions=(
            PreparedSubmitRevision(
                branch=branch,
                expected_remote_target="old-commit",
                remote_action="pushed",
                revision=revision,
            ),
        ),
        prior_reviewers={},
    )

    plan = plans[0]
    assert plan.action == "unchanged"
    assert plan.metadata is not None
    assert plan.metadata.labels == ["cli-label"]
    assert plan.metadata.reviewers == ["cli-user"]
    assert plan.metadata.team_reviewers == ["config-team"]


def _submit_options() -> SubmitOptions:
    return SubmitOptions(
        base_revset=None,
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
