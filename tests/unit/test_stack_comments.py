from __future__ import annotations

import asyncio
from typing import cast

import pytest

from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.stack_comments import (
    STACK_OVERVIEW_COMMENT_MARKER,
    delete_stack_overview_comment,
)
from jj_stack.models.github import GithubIssueComment


def _comment(*, body: str, comment_id: int) -> GithubIssueComment:
    return GithubIssueComment(
        body=body,
        databaseId=comment_id,
        url=f"https://github.test/comments/{comment_id}",
    )


class _CommentClient:
    def __init__(self, comments: tuple[GithubIssueComment, ...]) -> None:
        self.comments = comments
        self.deleted_ids: list[int] = []

    async def list_issue_comments(self, *, issue_number):
        assert issue_number == 3
        return self.comments

    async def delete_issue_comment(self, *, comment_id):
        self.deleted_ids.append(comment_id)


@pytest.mark.parametrize(
    "comments",
    (
        (_comment(body="marker removed", comment_id=7),),
        (_comment(body=STACK_OVERVIEW_COMMENT_MARKER, comment_id=8),),
    ),
    ids=("expected-marker-edited", "replacement-marker-created"),
)
def test_comment_delete_blocks_when_marker_plan_changes(
    comments: tuple[GithubIssueComment, ...],
) -> None:
    client = _CommentClient(comments)

    with pytest.raises(CliError, match="marker"):
        asyncio.run(
            delete_stack_overview_comment(
                comment_id=7,
                github_client=cast(GithubClient, client),
                pull_request_number=3,
            )
        )

    assert client.deleted_ids == []


def test_comment_delete_accepts_absent_target_only_without_replacement_marker() -> None:
    client = _CommentClient((_comment(body="ordinary comment", comment_id=8),))

    deleted = asyncio.run(
        delete_stack_overview_comment(
            comment_id=7,
            github_client=cast(GithubClient, client),
            pull_request_number=3,
        )
    )

    assert deleted is False
    assert client.deleted_ids == []
