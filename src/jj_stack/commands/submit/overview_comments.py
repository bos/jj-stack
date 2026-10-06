"""Synchronize submit stack overview comments on GitHub pull requests."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.concurrency import run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_LABEL,
    STACK_OVERVIEW_COMMENT_MARKER,
    delete_stack_overview_comment,
)
from jj_stack.models.github import GithubIssueComment

from .managed_comments import upsert_managed_comment
from .models import GeneratedDescription


async def sync_stack_overview_comments(
    *,
    comments_by_pr_number: dict[int, GithubIssueComment | None],
    overview_body: str | None,
    github_client: GithubClient,
    orphaned_pr_numbers: tuple[int, ...],
    pr_numbers: tuple[int, ...],
) -> None:
    """Write the planned overview to the head before removing its old copies."""
    head_pr_number = pr_numbers[-1]
    with console.progress(
        description="Syncing stack overview comments",
        total=len(pr_numbers) + len(orphaned_pr_numbers),
    ) as progress:
        await _sync_overview_comment(
            comment_body=overview_body,
            existing_comment=comments_by_pr_number.get(head_pr_number),
            github_client=github_client,
            pr_number=head_pr_number,
        )
        progress.advance()
        await run_bounded_tasks(
            items=(*pr_numbers[:-1], *orphaned_pr_numbers),
            run_item=lambda pr_number: _sync_overview_comment(
                comment_body=None,
                existing_comment=comments_by_pr_number.get(pr_number),
                github_client=github_client,
                pr_number=pr_number,
            ),
            on_success=progress.advance,
        )


def plan_stack_overview(
    *,
    comments: tuple[GithubIssueComment | None, ...],
    generated_stack_description: GeneratedDescription | None,
    is_lone_pr: bool,
    orphaned_comments: tuple[GithubIssueComment | None, ...],
) -> str | None:
    """Choose the overview from comments ordered bottom to head, then from orphaned PRs."""

    if is_lone_pr:
        return None
    if generated_stack_description is not None:
        description_lines = _render_generated_stack_description(generated_stack_description)
        return (
            "\n".join([STACK_OVERVIEW_COMMENT_MARKER, *description_lines])
            if description_lines
            else None
        )

    head_comment = comments[-1]
    if head_comment is not None:
        return head_comment.body

    existing_bodies = {
        comment.body for comment in (*comments, *orphaned_comments) if comment is not None
    }
    if len(existing_bodies) > 1:
        raise CliError(
            "Could not preserve the stack overview because pull requests in the stack "
            "have different managed comments.",
            hint=t"Write the combined stack overview to a file and add "
            t"{ui.cmd('--describe stack=FILE')} when retrying, replacing {ui.code('FILE')} "
            t"with that file's path.",
        )
    return next(iter(existing_bodies), None)


async def _sync_overview_comment(
    *,
    comment_body: str | None,
    existing_comment: GithubIssueComment | None,
    github_client: GithubClient,
    pr_number: int,
) -> None:
    if comment_body is None:
        if existing_comment is None:
            return
        await delete_stack_overview_comment(
            comment_id=existing_comment.id,
            github_client=github_client,
        )
        return
    await upsert_managed_comment(
        body=comment_body,
        existing_comment=existing_comment,
        github_client=github_client,
        label=STACK_OVERVIEW_COMMENT_LABEL,
        pr_number=pr_number,
    )


def _render_generated_stack_description(
    stack_description: GeneratedDescription,
) -> list[str]:
    lines: list[str] = []
    if stack_description.title:
        lines.append(f"## {stack_description.title}")
    if stack_description.body:
        if lines:
            lines.append("")
        lines.extend(stack_description.body.splitlines())
    return lines
