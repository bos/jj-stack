"""Synchronize submit stack comments on GitHub pull requests."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.concurrency import run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import (
    StackCommentKind,
    comment_matches_kind,
    delete_stack_comment,
    stack_comment_label,
    stack_comment_marker,
)
from jj_stack.models.github import GithubIssueComment

from .models import GeneratedDescription, SubmittedRevision


def navigation_comment_bodies(
    *,
    revisions: tuple[SubmittedRevision, ...],
    trunk_branch: str,
) -> dict[int, str | None]:
    """Return desired navigation-comment bodies keyed by pull request."""

    multi_change_stack = len(revisions) > 1
    bodies: dict[int, str | None] = {}
    for revision in revisions:
        if revision.pull_request_number is None:
            continue
        bodies[revision.pull_request_number] = (
            _render_navigation_comment(
                current=revision,
                revisions=revisions,
                trunk_branch=trunk_branch,
            )
            if multi_change_stack
            else None
        )
    return bodies


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
        "\n".join([stack_comment_marker("overview"), *description_lines])
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


async def sync_stack_comments(
    *,
    concurrency: int,
    github_client: GithubClient,
    navigation_bodies: dict[int, str | None],
    overview_bodies: dict[int, str | None],
) -> None:
    """Synchronize the supplied navigation and overview responsibilities."""

    pull_request_numbers = tuple(dict.fromkeys((*navigation_bodies, *overview_bodies)))
    if not pull_request_numbers:
        return
    with console.spinner(description="Loading stack comments"):
        try:
            comments_by_pull_request_number = (
                await github_client.get_issue_comments_by_pull_request_numbers(
                    pull_numbers=pull_request_numbers,
                )
            )
        except GithubClientError as error:
            raise CliError("Could not list stack comments") from error

    with console.progress(
        description="Syncing stack comments",
        total=len(pull_request_numbers),
    ) as progress:
        await run_bounded_tasks(
            concurrency=concurrency,
            items=pull_request_numbers,
            run_item=lambda pull_request_number: _sync_stack_comment_task(
                github_client=github_client,
                comments=comments_by_pull_request_number[pull_request_number],
                navigation_bodies=navigation_bodies,
                overview_bodies=overview_bodies,
                pull_request_number=pull_request_number,
            ),
            on_success=lambda _index, _result: progress.advance(),
        )


async def _sync_stack_comment_task(
    *,
    comments: tuple[GithubIssueComment, ...],
    github_client: GithubClient,
    navigation_bodies: dict[int, str | None],
    overview_bodies: dict[int, str | None],
    pull_request_number: int,
) -> None:
    if pull_request_number in navigation_bodies:
        await _sync_managed_comment(
            comment_body=navigation_bodies[pull_request_number],
            comments=comments,
            github_client=github_client,
            kind="navigation",
            pull_request_number=pull_request_number,
        )
    if pull_request_number in overview_bodies:
        await _sync_managed_comment(
            comment_body=overview_bodies[pull_request_number],
            comments=comments,
            github_client=github_client,
            kind="overview",
            pull_request_number=pull_request_number,
        )


async def _sync_managed_comment(
    *,
    comment_body: str | None,
    comments: tuple[GithubIssueComment, ...],
    github_client: GithubClient,
    kind: StackCommentKind,
    pull_request_number: int,
) -> GithubIssueComment | None:
    existing_comment = _discover_managed_comment(
        comments=comments,
        kind=kind,
    )
    if comment_body is None:
        if existing_comment is None:
            return None
        await delete_stack_comment(
            comment_id=existing_comment.id,
            github_client=github_client,
            kind=kind,
            pull_request_number=pull_request_number,
        )
        return None
    if existing_comment is not None:
        if existing_comment.body == comment_body:
            return existing_comment
        return await _update_stack_comment(
            comment_body=comment_body,
            comment_id=existing_comment.id,
            github_client=github_client,
            kind=kind,
        )
    return await _create_stack_comment(
        comment_body=comment_body,
        github_client=github_client,
        kind=kind,
        pull_request_number=pull_request_number,
    )


def _discover_managed_comment(
    *,
    comments: tuple[GithubIssueComment, ...],
    kind: StackCommentKind,
) -> GithubIssueComment | None:
    matching_comments = [
        comment for comment in comments if comment_matches_kind(body=comment.body, kind=kind)
    ]
    if not matching_comments:
        return None
    if len(matching_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in matching_comments)
        raise CliError(
            t"GitHub reports multiple jj-stack {stack_comment_label(kind)}s for the same "
            t"pull request: {comment_ids}.",
            hint=(
                t"Inspect the PR link with {ui.cmd('view --fetch')} or delete the "
                t"extra {stack_comment_label(kind)}s before submitting again."
            ),
        )
    return matching_comments[0]


async def _create_stack_comment(
    *,
    comment_body: str,
    github_client: GithubClient,
    kind: StackCommentKind,
    pull_request_number: int,
) -> GithubIssueComment:
    try:
        return await github_client.create_issue_comment(
            issue_number=pull_request_number,
            body=comment_body,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not create a {stack_comment_label(kind)} for pull request "
            f"#{pull_request_number}"
        ) from error


async def _update_stack_comment(
    *,
    comment_body: str,
    comment_id: int,
    github_client: GithubClient,
    kind: StackCommentKind,
) -> GithubIssueComment:
    try:
        return await github_client.update_issue_comment(
            comment_id=comment_id,
            body=comment_body,
        )
    except GithubClientError as error:
        raise CliError(f"Could not update {stack_comment_label(kind)} #{comment_id}") from error


def _render_navigation_comment(
    *,
    current: SubmittedRevision,
    revisions: tuple[SubmittedRevision, ...],
    trunk_branch: str,
) -> str:
    lines = [stack_comment_marker("navigation")]
    lines.extend(
        [
            "This pull request is part of a stack tracked by `jj-stack`.",
            "",
            "Stack:",
        ]
    )
    for revision in reversed(revisions):
        title = revision.pull_request_title or revision.prepared.revision.subject
        if revision.change_id == current.change_id:
            lines.append(f"**{title} (this PR)**")
        elif revision.pull_request_url is None:
            lines.append(title)
        else:
            lines.append(f"[{title}]({revision.pull_request_url})")
    lines.append(f"trunk `{trunk_branch}`")
    return "\n".join(lines)


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
