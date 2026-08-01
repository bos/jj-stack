"""Shared helpers for GitHub stack overview comments."""

from __future__ import annotations

from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError

STACK_OVERVIEW_COMMENT_LABEL = "stack overview comment"
STACK_OVERVIEW_COMMENT_MARKER = "<!-- jj-stack-overview -->"


def is_overview_comment(body: str) -> bool:
    """Return whether a GitHub comment body is a managed overview comment."""

    return STACK_OVERVIEW_COMMENT_MARKER in body


async def delete_stack_overview_comment(
    *,
    comment_id: int,
    github_client: GithubClient,
    pull_request_number: int,
) -> bool:
    """Re-observe and delete one exact managed comment.

    Returns whether the expected comment still existed and was deleted. An
    already-absent target is complete only while no replacement marker exists.
    """

    try:
        comments = await github_client.list_issue_comments(
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not verify {STACK_OVERVIEW_COMMENT_LABEL} #{comment_id} on "
            t"PR #{pull_request_number}"
        ) from error

    expected_comment = next((comment for comment in comments if comment.id == comment_id), None)
    matching_comments = tuple(
        comment for comment in comments if is_overview_comment(comment.body)
    )
    if expected_comment is None:
        if not matching_comments:
            return False
        raise CliError(
            t"Cannot delete {STACK_OVERVIEW_COMMENT_LABEL} #{comment_id} because its marker "
            t"now belongs to a different or ambiguous comment on PR #{pull_request_number}."
        )
    if len(matching_comments) != 1 or matching_comments[0].id != comment_id:
        raise CliError(
            t"Cannot delete {STACK_OVERVIEW_COMMENT_LABEL} #{comment_id} because its marker "
            t"changed or became ambiguous on PR #{pull_request_number}."
        )

    try:
        await github_client.delete_issue_comment(
            comment_id=comment_id,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return False
        raise CliError(
            f"Could not delete {STACK_OVERVIEW_COMMENT_LABEL} #{comment_id}"
        ) from error
    return True
