"""Synchronize submit stack overview comments on GitHub pull requests."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.concurrency import run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_LABEL,
    STACK_OVERVIEW_COMMENT_MARKER,
    delete_stack_overview_comment,
    is_overview_comment,
)
from jj_stack.models.github import GithubIssueComment

from .models import GeneratedDescription, SubmittedRevision


def stack_overview_comment_bodies(
    *,
    generated_stack_description: GeneratedDescription | None,
    revisions: tuple[SubmittedRevision, ...],
) -> dict[int, str | None]:
    """Return desired stack-overview bodies keyed by pull request."""

    if not revisions:
        return {}
    description_lines = _render_generated_stack_description(generated_stack_description)
    overview_body = (
        "\n".join([STACK_OVERVIEW_COMMENT_MARKER, *description_lines])
        if len(revisions) > 1 and description_lines
        else None
    )
    head_change_id = revisions[-1].change_id
    return {
        revision.pull_request_number: (
            overview_body if revision.change_id == head_change_id else None
        )
        for revision in revisions
        if revision.pull_request_number is not None
    }


async def sync_stack_overview_comments(
    *,
    concurrency: int,
    github_client: GithubClient,
    overview_bodies: dict[int, str | None],
) -> None:
    """Synchronize the supplied stack-overview responsibilities."""

    pull_request_numbers = tuple(overview_bodies)
    if not pull_request_numbers:
        return
    with console.spinner(description="Loading stack overview comments"):
        try:
            comments_by_pull_request_number = (
                await github_client.get_issue_comments_by_pull_request_numbers(
                    pull_numbers=pull_request_numbers,
                )
            )
        except GithubClientError as error:
            raise CliError("Could not list stack overview comments") from error

    with console.progress(
        description="Syncing stack overview comments",
        total=len(pull_request_numbers),
    ) as progress:
        await run_bounded_tasks(
            concurrency=concurrency,
            items=pull_request_numbers,
            run_item=lambda pull_request_number: _sync_overview_comment(
                comment_body=overview_bodies[pull_request_number],
                comments=comments_by_pull_request_number[pull_request_number],
                github_client=github_client,
                pull_request_number=pull_request_number,
            ),
            on_success=lambda _index, _result: progress.advance(),
        )


async def _sync_overview_comment(
    *,
    comment_body: str | None,
    comments: tuple[GithubIssueComment, ...],
    github_client: GithubClient,
    pull_request_number: int,
) -> GithubIssueComment | None:
    existing_comment = _discover_overview_comment(comments=comments)
    if comment_body is None:
        if existing_comment is None:
            return None
        await delete_stack_overview_comment(
            comment_id=existing_comment.id,
            github_client=github_client,
            pull_request_number=pull_request_number,
        )
        return None
    if existing_comment is not None:
        if existing_comment.body == comment_body:
            return existing_comment
        return await _update_stack_overview_comment(
            comment_body=comment_body,
            comment_id=existing_comment.id,
            github_client=github_client,
        )
    return await _create_stack_overview_comment(
        comment_body=comment_body,
        github_client=github_client,
        pull_request_number=pull_request_number,
    )


def _discover_overview_comment(
    *,
    comments: tuple[GithubIssueComment, ...],
) -> GithubIssueComment | None:
    matching_comments = [comment for comment in comments if is_overview_comment(comment.body)]
    if not matching_comments:
        return None
    if len(matching_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in matching_comments)
        raise CliError(
            t"GitHub reports multiple jj-stack {STACK_OVERVIEW_COMMENT_LABEL}s for the same "
            t"pull request: {comment_ids}.",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} or delete the "
                t"extra {STACK_OVERVIEW_COMMENT_LABEL}s before submitting again."
            ),
        )
    return matching_comments[0]


async def _create_stack_overview_comment(
    *,
    comment_body: str,
    github_client: GithubClient,
    pull_request_number: int,
) -> GithubIssueComment:
    try:
        return await github_client.create_issue_comment(
            issue_number=pull_request_number,
            body=comment_body,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not create a {STACK_OVERVIEW_COMMENT_LABEL} for pull request "
            f"#{pull_request_number}"
        ) from error


async def _update_stack_overview_comment(
    *,
    comment_body: str,
    comment_id: int,
    github_client: GithubClient,
) -> GithubIssueComment:
    try:
        return await github_client.update_issue_comment(
            comment_id=comment_id,
            body=comment_body,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not update {STACK_OVERVIEW_COMMENT_LABEL} #{comment_id}"
        ) from error


def _render_generated_stack_description(
    stack_description: GeneratedDescription | None,
) -> list[str]:
    if stack_description is None:
        return []

    lines: list[str] = []
    if stack_description.title:
        lines.append(f"## {stack_description.title}")
    if stack_description.body:
        if lines:
            lines.append("")
        lines.extend(stack_description.body.splitlines())
    return lines
