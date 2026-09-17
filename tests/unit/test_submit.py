from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from jj_stack.commands.submit.changes import prepare_submit_changes
from jj_stack.commands.submit.command import _pr_metadata
from jj_stack.commands.submit.inputs import preflight_private_commits
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.overview_comments import sync_stack_overview_comments
from jj_stack.commands.submit.revision_comments import _include_submitted_force_push
from jj_stack.config import AppConfig
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import CommitId
from jj_stack.jj.client import JjClient
from jj_stack.models.github import (
    GithubIssueComment,
    GithubPRRevision,
)
from jj_stack.models.stack import LocalStack
from jj_stack.stack.change_state import ChangeObservation
from jj_stack.stack.pr_branches import ResolvedPRBranch
from jj_stack.stack.status import discover_pr_lookups
from tests.support.change_helpers import make_change
from tests.support.contexts import fake_command_context


def test_overview_comment_move_keeps_source_when_head_creation_fails() -> None:
    source_comment = GithubIssueComment(
        body="<!-- jj-stack-overview -->\nEdited",
        databaseId=7,
    )

    class CommentClientStub(GithubClient):
        def __init__(self) -> None:
            self.deleted_comment_ids: list[int] = []
            self._repo = GithubRepoAddress(owner="octo-org", repo="stacked-prs")

        async def create_issue_comment(
            self,
            *,
            issue_number: int,
            body: str,
        ) -> None:
            assert issue_number == 2
            assert body == source_comment.body
            raise GithubClientError("create failed")

        async def delete_issue_comment(self, *, comment_id: int) -> None:
            self.deleted_comment_ids.append(comment_id)

    client = CommentClientStub()

    with pytest.raises(CliError, match="Could not create a stack overview comment"):
        asyncio.run(
            sync_stack_overview_comments(
                comments_by_pr_number={1: source_comment, 2: None},
                concurrency=2,
                overview_body=source_comment.body,
                github_client=client,
                pr_numbers=(1, 2),
            )
        )

    assert client.deleted_comment_ids == []


def test_first_submit_stops_when_github_rejects_the_open_pr_lookup() -> None:
    change = make_change(commit_id="current", change_id="abcdefghijk", description="feature\n")
    trunk = make_change(commit_id="trunk", change_id="trunk-change", description="base\n")
    branch = "jj-stack/feature-abcdefgh"

    github = Mock(spec=GithubClient)
    github.get_open_prs_by_head_refs.side_effect = GithubClientError(
        "invalid lookup", status_code=422
    )

    lookup = asyncio.run(
        discover_pr_lookups(
            github_client=github,
            observations={
                branch: ChangeObservation(
                    change_id=change.change_id,
                    branch=branch,
                    tracked=None,
                    selected=change,
                    local=(change,),
                )
            },
        )
    )[branch]

    with pytest.raises(CliError, match="GitHub 422"):
        prepare_submit_changes(
            branch_resolutions=(ResolvedPRBranch(branch=branch, change_id=change.change_id),),
            lookups={branch: lookup},
            remote_targets={branch: change.commit_id},
            stack=LocalStack(
                base_parent=trunk,
                head=change,
                changes=(change,),
                selected_revset=change.change_id,
                trunk=trunk,
            ),
        )


def test_preflight_private_commits_rejects_blocked_change() -> None:
    private = make_change(
        commit_id="head",
        change_id="head-change",
        description="private thing\n",
    )
    client = Mock(spec=JjClient)
    client.find_private_commits.return_value = (private,)

    with pytest.raises(CliError, match="git.private-commits"):
        preflight_private_commits(client, (private,))


def test_submit_metadata_prefers_cli_values_over_config() -> None:
    metadata = _pr_metadata(
        context=fake_command_context(
            config=AppConfig(
                labels=["config-label"],
                reviewers=["config-user"],
                team_reviewers=["config-team"],
            ),
        ),
        options=SubmitOptions(
            base_revset=None,
            descriptions=(),
            describe_with=None,
            draft_mode="default",
            dry_run=False,
            edit=False,
            labels=["cli-label"],
            re_request=False,
            reviewers=["cli-user"],
            revset="@",
            team_reviewers=None,
        ),
    )

    assert metadata.labels == ["cli-label"]
    assert metadata.reviewers == ["cli-user"]
    assert metadata.team_reviewers == ["config-team"]


def test_revision_history_fills_only_the_force_push_github_has_not_indexed() -> None:
    def revision(version: int, before: str, after: str, *, current: bool) -> GithubPRRevision:
        return GithubPRRevision(
            before_commit_id=before, commit_id=after, is_current=current, version=version
        )

    indexed = (revision(2, "c1", "c2", current=True),)

    assert _include_submitted_force_push(indexed, None) == indexed
    assert _include_submitted_force_push(indexed, (CommitId("c1"), CommitId("c2"))) == indexed
    # A push that does not continue the indexed history is not this PR's next revision.
    assert _include_submitted_force_push(indexed, (CommitId("c9"), CommitId("c3"))) == indexed
    assert _include_submitted_force_push(indexed, (CommitId("c2"), CommitId("c3"))) == (
        revision(2, "c1", "c2", current=False),
        revision(3, "c2", "c3", current=True),
    )
    assert _include_submitted_force_push((), (CommitId("c1"), CommitId("c2"))) == (
        revision(2, "c1", "c2", current=True),
    )
