"""Synchronize submit stack comments on GitHub pull requests."""

from __future__ import annotations

import jj_review.console as console
import jj_review.ui as ui
from jj_review.commands._close_actions import (
    comment_matches_kind as _managed_comment_matches_kind,
)
from jj_review.concurrency import run_bounded_tasks
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.github.stack_comments import (
    StackCommentKind,
    stack_comment_label,
    stack_comment_marker,
)
from jj_review.models.github import GithubIssueComment
from jj_review.models.review_state import CachedChange

from .models import (
    GeneratedDescription,
    PendingStackCommentSync,
    SubmitMutationRun,
    SubmittedRevision,
)


async def sync_stack_comments(
    *,
    concurrency: int,
    generated_stack_description: GeneratedDescription | None,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    revisions: tuple[SubmittedRevision, ...],
    run: SubmitMutationRun,
    trunk_branch: str,
) -> None:
    """Synchronize navigation and overview comments for a submitted stack."""

    if not revisions:
        return

    head_change_id = revisions[-1].change_id
    has_navigation_comments = len(revisions) > 1
    overview_description_lines = _render_generated_stack_description(
        generated_stack_description
    )
    has_overview_comment = has_navigation_comments and bool(overview_description_lines)
    pending: list[PendingStackCommentSync] = []
    for revision in revisions:
        if revision.pull_request_number is None:
            continue
        cached_change = (
            run.state_changes.get(revision.change_id)
            or run.state.changes.get(revision.change_id)
        )
        if cached_change is None:
            if run.dry_run:
                continue
            raise AssertionError("Stack summary comments require a saved pull request link.")
        navigation_comment_body = None
        if has_navigation_comments:
            navigation_comment_body = _render_navigation_comment(
                current=revision,
                revisions=revisions,
                trunk_branch=trunk_branch,
            )
        overview_comment_body = None
        if has_overview_comment and revision.change_id == head_change_id:
            overview_comment_body = "\n".join(
                [stack_comment_marker("overview"), *overview_description_lines]
            )
        pending.append(
            PendingStackCommentSync(
                cached_change=cached_change,
                change_id=revision.change_id,
                navigation_comment_body=navigation_comment_body,
                overview_comment_body=overview_comment_body,
                pull_request_number=revision.pull_request_number,
            )
        )
    if not pending:
        return
    with console.spinner(description="Loading stack comments"):
        try:
            comments_by_pull_request_number = (
                await github_client.get_issue_comments_by_pull_request_numbers(
                    github_repository.owner,
                    github_repository.repo,
                    pull_numbers=tuple(
                        pending_sync.pull_request_number for pending_sync in pending
                    ),
                )
            )
        except GithubClientError as error:
            raise CliError("Could not list stack comments") from error

    with console.progress(description="Syncing stack comments", total=len(pending)) as progress:

        def handle_success(_index: int, result: tuple[str, CachedChange]) -> None:
            change_id, updated_change = result
            previous_change = run.state_changes.get(change_id) or run.state.changes.get(change_id)
            if previous_change != updated_change:
                run.state_changes[change_id] = updated_change
                run.record_saved_state_update(
                    after=updated_change,
                    before=previous_change,
                    change_id=change_id,
                )
                run.save_interim_state()
            progress.advance()

        await run_bounded_tasks(
            concurrency=concurrency,
            items=tuple(pending),
            run_item=lambda pending_sync: _sync_stack_comment_task(
                github_client=github_client,
                github_repository=github_repository,
                comments=comments_by_pull_request_number[pending_sync.pull_request_number],
                pending_sync=pending_sync,
                run=run,
            ),
            on_success=handle_success,
        )


async def _sync_stack_comment_task(
    *,
    comments: tuple[GithubIssueComment, ...],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pending_sync: PendingStackCommentSync,
    run: SubmitMutationRun,
) -> tuple[str, CachedChange]:
    navigation_comment = await _sync_managed_comment(
        cached_comment_id=pending_sync.cached_change.navigation_comment_id,
        comment_body=pending_sync.navigation_comment_body,
        comments=comments,
        github_client=github_client,
        github_repository=github_repository,
        kind="navigation",
        pull_request_number=pending_sync.pull_request_number,
        run=run,
    )
    overview_comment = await _sync_managed_comment(
        cached_comment_id=pending_sync.cached_change.overview_comment_id,
        comment_body=pending_sync.overview_comment_body,
        comments=comments,
        github_client=github_client,
        github_repository=github_repository,
        kind="overview",
        pull_request_number=pending_sync.pull_request_number,
        run=run,
    )
    updated_change = pending_sync.cached_change.model_copy(
        update={
            "navigation_comment_id": (
                None if navigation_comment is None else navigation_comment.id
            ),
            "overview_comment_id": None if overview_comment is None else overview_comment.id,
        }
    )
    return pending_sync.change_id, updated_change


async def _sync_managed_comment(
    *,
    cached_comment_id: int | None,
    comment_body: str | None,
    comments: tuple[GithubIssueComment, ...],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    kind: StackCommentKind,
    pull_request_number: int,
    run: SubmitMutationRun,
) -> GithubIssueComment | None:
    dry_run = run.dry_run
    existing_comment = _resolve_saved_managed_comment(
        cached_comment_id=cached_comment_id,
        comments=comments,
        kind=kind,
        pull_request_number=pull_request_number,
    )
    if comment_body is None:
        if existing_comment is None:
            return None
        if not dry_run:
            await _delete_stack_comment(
                comment_id=existing_comment.id,
                github_client=github_client,
                github_repository=github_repository,
                kind=kind,
            )
        return None
    if existing_comment is not None:
        if existing_comment.body == comment_body:
            return existing_comment
        if dry_run:
            return existing_comment
        return await _update_stack_comment(
            comment_body=comment_body,
            comment_id=existing_comment.id,
            github_client=github_client,
            github_repository=github_repository,
            kind=kind,
        )
    if dry_run:
        return None
    return await _create_stack_comment(
        comment_body=comment_body,
        github_client=github_client,
        github_repository=github_repository,
        kind=kind,
        pull_request_number=pull_request_number,
    )


def _resolve_saved_managed_comment(
    *,
    cached_comment_id: int | None,
    comments: tuple[GithubIssueComment, ...],
    kind: StackCommentKind,
    pull_request_number: int,
) -> GithubIssueComment | None:
    if cached_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_comment_id),
            None,
        )
        if cached_comment is not None:
            if not _managed_comment_matches_kind(body=cached_comment.body, kind=kind):
                raise CliError(
                    t"Saved {stack_comment_label(kind)} #{cached_comment_id} for pull request "
                    t"#{pull_request_number} does not belong to jj-review.",
                    hint=(
                        t"Inspect the PR link with {ui.cmd('view --fetch')} or "
                        t"delete the saved comment ID before submitting again."
                    ),
                )
            return cached_comment
    return _discover_managed_comment(
        comments=comments,
        kind=kind,
    )


def _discover_managed_comment(
    *,
    comments: tuple[GithubIssueComment, ...],
    kind: StackCommentKind,
) -> GithubIssueComment | None:
    matching_comments = [
        comment
        for comment in comments
        if _managed_comment_matches_kind(body=comment.body, kind=kind)
    ]
    if not matching_comments:
        return None
    if len(matching_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in matching_comments)
        raise CliError(
            t"GitHub reports multiple jj-review {stack_comment_label(kind)}s for the same "
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
    github_repository: ParsedGithubRepo,
    kind: StackCommentKind,
    pull_request_number: int,
) -> GithubIssueComment:
    try:
        return await github_client.create_issue_comment(
            github_repository.owner,
            github_repository.repo,
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
    github_repository: ParsedGithubRepo,
    kind: StackCommentKind,
) -> GithubIssueComment:
    try:
        return await github_client.update_issue_comment(
            github_repository.owner,
            github_repository.repo,
            comment_id=comment_id,
            body=comment_body,
        )
    except GithubClientError as error:
        raise CliError(f"Could not update {stack_comment_label(kind)} #{comment_id}") from error


async def _delete_stack_comment(
    *,
    comment_id: int,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    kind: StackCommentKind,
) -> None:
    try:
        await github_client.delete_issue_comment(
            github_repository.owner,
            github_repository.repo,
            comment_id=comment_id,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return
        raise CliError(f"Could not delete {stack_comment_label(kind)} #{comment_id}") from error


def _render_navigation_comment(
    *,
    current: SubmittedRevision,
    revisions: tuple[SubmittedRevision, ...],
    trunk_branch: str,
) -> str:
    lines = [stack_comment_marker("navigation")]
    lines.extend(
        [
            "This pull request is part of a stack tracked by `jj-review`.",
            "",
            "Stack:",
        ]
    )
    for revision in reversed(revisions):
        title = revision.pull_request_title or revision.subject
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
