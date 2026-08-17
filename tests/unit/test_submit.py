from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.submit.changes import prepare_submit_changes
from jj_stack.commands.submit.command import (
    _pr_sync_plans,
)
from jj_stack.commands.submit.inputs import preflight_private_commits
from jj_stack.commands.submit.models import (
    GeneratedDescription,
    PreparedSubmitChange,
    SubmitOptions,
)
from jj_stack.commands.submit.overview_comments import sync_stack_overview_comments
from jj_stack.commands.submit.prs import (
    _select_discovered_pr,
    ensure_pr_link_is_consistent,
)
from jj_stack.config import AppConfig
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import (
    GithubBranchRef,
    GithubIssueComment,
    GithubPR,
)
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import (
    PRIdentity,
    SubmittedBaseline,
    TrackedPR,
    TrackingState,
)
from jj_stack.stack.pr_branches import ResolvedPRBranch
from tests.support.change_helpers import make_change
from tests.support.tracking import make_pr_identity

_REMOTE_URL = "https://github.test/octo-org/repo.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)


def test_overview_comment_sync_batches_comment_reads() -> None:
    class CommentClientStub(GithubClient):
        def __init__(self) -> None:
            self.comment_batches: list[tuple[int, ...]] = []

        async def get_issue_comments_by_pr_numbers(
            self,
            *,
            pr_numbers: Sequence[int],
        ) -> dict[int, tuple[GithubIssueComment, ...]]:
            self.comment_batches.append(tuple(pr_numbers))
            return {number: () for number in pr_numbers}

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


def test_prepare_submit_changes_rejects_saved_remote_branch_drift() -> None:
    change = make_change(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    identity = make_pr_identity(
        head_ref="jj-stack/feature-abcdefgh",
        pr_number=17,
    )

    with pytest.raises(CliError, match="unexpected commit"):
        prepare_submit_changes(
            branch_resolutions=(
                ResolvedPRBranch(
                    branch=identity.head_ref,
                    change_id=change.change_id,
                ),
            ),
            remote_targets={identity.head_ref: "external-commit"},
            remote=_REMOTE,
            stack=_local_stack(change),
            state=TrackingState(
                pr_identities={change.change_id: identity},
                submitted_baselines={
                    change.change_id: SubmittedBaseline(commit_id="submitted-commit")
                },
            ),
        )


def test_prepare_submit_changes_rejects_unclaimed_existing_branch() -> None:
    change = make_change(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    branch = "jj-stack/feature-abcdefgh"

    with pytest.raises(CliError, match="already exists"):
        prepare_submit_changes(
            branch_resolutions=(
                ResolvedPRBranch(
                    branch=branch,
                    change_id=change.change_id,
                ),
            ),
            remote_targets={branch: "another-commit"},
            remote=_REMOTE,
            stack=_local_stack(change),
            state=TrackingState(),
        )


def test_prepare_submit_changes_requires_recovered_branch_lease_to_stay_exact() -> None:
    change = make_change(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    branch = "jj-stack/older-title-abcdefgh"

    with pytest.raises(CliError, match="changed during submission"):
        prepare_submit_changes(
            branch_resolutions=(
                ResolvedPRBranch(
                    branch=branch,
                    change_id=change.change_id,
                    recovered_target="interrupted-commit",
                ),
            ),
            remote_targets={branch: "external-commit"},
            remote=_REMOTE,
            stack=_local_stack(change),
            state=TrackingState(),
        )


def test_pr_link_rejects_missing_discovered_pr() -> None:
    identity = make_pr_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)

    with pytest.raises(CliError, match="GitHub no longer reports a PR"):
        ensure_pr_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pr=None,
            expected_remote_target="commit-17",
            repo_key=("octo-org", "stacked-prs"),
            tracked_pr=_tracked_pr(identity),
        )


def test_pr_link_rejects_a_saved_pr_from_another_repo() -> None:
    identity = make_pr_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)

    with pytest.raises(CliError, match="belongs to a different GitHub repo"):
        ensure_pr_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pr=None,
            expected_remote_target="commit-17",
            repo_key=("octo-org", "other-repo"),
            tracked_pr=_tracked_pr(identity),
        )


def test_pr_link_rejects_remote_and_pr_head_mismatch() -> None:
    identity = make_pr_identity(head_ref="jj-stack/foo-abcdefgh", pr_number=17)
    pr = _github_pr(
        number=17,
        branch=identity.head_ref,
        head_sha="github-commit",
    )

    with pytest.raises(CliError, match="remote branch no longer identify the same commit"):
        ensure_pr_link_is_consistent(
            branch=identity.head_ref,
            change_id="abcdefghijk",
            discovered_pr=pr,
            expected_remote_target="remote-commit",
            repo_key=("octo-org", "stacked-prs"),
            tracked_pr=_tracked_pr(identity, commit_id="remote-commit"),
        )


def _tracked_pr(
    identity: PRIdentity,
    *,
    commit_id: str = "commit-17",
) -> TrackedPR:
    return TrackedPR(
        change_id="abcdefghijk",
        pr_identity=identity,
        submitted_baseline=SubmittedBaseline(commit_id=commit_id),
    )


def test_preflight_private_commits_rejects_blocked_change() -> None:
    private = make_change(
        commit_id="head",
        change_id="head-change",
        description="private thing\n",
    )

    class PrivateCommitClient:
        def find_private_commits(
            self,
            changes: tuple[LocalCommit, ...],
        ) -> tuple[LocalCommit, ...]:
            del changes
            return (private,)

    with pytest.raises(CliError, match="git.private-commits"):
        preflight_private_commits(PrivateCommitClient(), (private,))


def test_discovered_pr_must_have_only_one_open_pr() -> None:
    prs = tuple(
        _github_pr(number=number, state=state)
        for number, state in enumerate(("open", "open"), start=1)
    )
    with pytest.raises(CliError, match="multiple pull requests"):
        _select_discovered_pr(
            head_label="octo-org:jj-stack/foo",
            prs=prs,
            tracked_pr_number=None,
        )


def test_pr_plan_prefers_cli_metadata_over_config() -> None:
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

    change = make_change(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    branch = "jj-stack/feature-abcdefgh"
    plans = _pr_sync_plans(
        bottom_base_branch="main",
        context=context,
        discovered_prs={branch: _github_pr(number=17, branch=branch)},
        drafts={change.change_id: False},
        generated_descriptions={change.change_id: GeneratedDescription(body="", title="feature")},
        options=replace(
            _submit_options(),
            labels=["cli-label"],
            reviewers=["cli-user"],
        ),
        prepared_changes=(
            PreparedSubmitChange(
                branch=branch,
                expected_remote_target="old-commit",
                remote_action="pushed",
                change=change,
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


def _local_stack(*changes: LocalCommit) -> LocalStack:
    trunk = make_change(
        commit_id="trunk",
        change_id="trunk-change",
        description="base\n",
    )
    return LocalStack(
        base_parent=trunk,
        head=changes[-1],
        changes=changes,
        selected_revset=changes[-1].change_id,
        trunk=trunk,
    )


def _github_pr(
    number: int,
    *,
    branch: str = "jj-stack/foo",
    head_sha: str = "head-commit",
    state: str = "open",
) -> GithubPR:
    return GithubPR(
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
