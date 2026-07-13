"""Finalize one landed pull request through the selected GitHub transport."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import StackCommentKind, delete_stack_comment
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import CachedChange

from .models import LandAction, LandRevision


async def finalize_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    landed_revision: LandRevision,
    trunk_branch: str,
) -> GithubPullRequest:
    """Retarget and close a PR whose exact commit reached trunk directly."""

    try:
        pull_request = await github_client.get_pull_request(
            pull_number=landed_revision.pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not load PR #{landed_revision.pull_request_number} during land"
        ) from error
    pull_request = pull_request.normalize_state()
    _ensure_landed_pull_request_head(
        github_client=github_client,
        landed_revision=landed_revision,
        pull_request=pull_request,
    )
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                pull_number=pull_request.number,
                base=trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to "
                t"{ui.bookmark(trunk_branch)}"
            ) from error
        pull_request = pull_request.normalize_state()
        _ensure_landed_pull_request_head(
            github_client=github_client,
            landed_revision=landed_revision,
            pull_request=pull_request,
        )
    if pull_request.state == "open":
        try:
            await github_client.close_pull_request(
                pull_number=pull_request.number,
            )
            pull_request = await github_client.get_pull_request(
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            recovered_pull_request: GithubPullRequest | None = None
            if error.status_code == 422:
                try:
                    refreshed_pull_request = await github_client.get_pull_request(
                        pull_number=pull_request.number,
                    )
                except GithubClientError:
                    pass
                else:
                    refreshed_pull_request = refreshed_pull_request.normalize_state()
                    if refreshed_pull_request.state == "merged":
                        recovered_pull_request = refreshed_pull_request
            if recovered_pull_request is None:
                raise CliError(
                    t"Could not close PR #{pull_request.number} after landing"
                ) from error
            pull_request = recovered_pull_request
        pull_request = pull_request.normalize_state()
        _ensure_landed_pull_request_head(
            github_client=github_client,
            landed_revision=landed_revision,
            pull_request=pull_request,
        )
        if pull_request.state == "open":
            raise CliError(
                t"Cannot finalize PR #{pull_request.number} because GitHub still "
                t"reports it open after the close request.",
                hint=t"Inspect the PR on GitHub and rerun {ui.cmd('land')}.",
            )
    await _delete_landed_stack_comments(
        cached_change=cached_change,
        github_client=github_client,
    )
    return pull_request


async def merge_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    landed_revision: LandRevision,
    merge_method: str,
    trunk_branch: str,
) -> tuple[GithubPullRequest | None, LandAction | None]:
    """Retarget one landable PR to trunk and merge it through the GitHub API.

    Returns the merged pull request, or a blocking action when GitHub refuses
    the merge (pending checks, conflicts, or repo policy).
    """

    try:
        pull_request = await github_client.get_pull_request(
            pull_number=landed_revision.pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not load PR #{landed_revision.pull_request_number} during land"
        ) from error
    pull_request = pull_request.normalize_state()
    _ensure_landed_pull_request_head(
        github_client=github_client,
        landed_revision=landed_revision,
        pull_request=pull_request,
    )
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                pull_number=pull_request.number,
                base=trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to "
                t"{ui.bookmark(trunk_branch)}"
            ) from error
        pull_request = pull_request.normalize_state()
        _ensure_landed_pull_request_head(
            github_client=github_client,
            landed_revision=landed_revision,
            pull_request=pull_request,
        )
    if pull_request.state == "open":
        try:
            await github_client.merge_pull_request(
                pull_number=pull_request.number,
                merge_method=merge_method,
            )
        except GithubClientError as error:
            if error.status_code in (405, 409):
                return None, LandAction(
                    kind="boundary",
                    body=t"at PR #{pull_request.number} for {landed_revision.subject} "
                    t"{ui.change_id(landed_revision.change_id)}: GitHub reports it is "
                    t"not mergeable (pending checks, conflicts, or repo policy); make "
                    t"it mergeable and rerun {ui.cmd('land --via merge')}",
                    status="blocked",
                )
            raise CliError(
                t"Could not merge PR #{pull_request.number} on GitHub"
            ) from error
        try:
            pull_request = await github_client.get_pull_request(
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not reload PR #{pull_request.number} after merging"
            ) from error
        pull_request = pull_request.normalize_state()
        _ensure_landed_pull_request_head(
            github_client=github_client,
            landed_revision=landed_revision,
            pull_request=pull_request,
        )
    if pull_request.state != "merged":
        return None, LandAction(
            kind="boundary",
            body=t"at PR #{pull_request.number} for {landed_revision.subject} "
            t"{ui.change_id(landed_revision.change_id)}: the PR is "
            t"{pull_request.state} instead of merged; inspect it on GitHub and "
            t"rerun {ui.cmd('land --via merge')}",
            status="blocked",
        )
    await _delete_landed_stack_comments(
        cached_change=cached_change,
        github_client=github_client,
    )
    return pull_request, None


def _ensure_landed_pull_request_head(
    *,
    github_client: GithubClient,
    landed_revision: LandRevision,
    pull_request: GithubPullRequest,
) -> None:
    expected_head_label = f"{github_client.repository.owner}:{landed_revision.bookmark}"
    if (
        pull_request.head.ref == landed_revision.bookmark
        and pull_request.head.label == expected_head_label
        and pull_request.head.sha == landed_revision.commit_id
    ):
        return
    raise CliError(
        t"Cannot finalize PR #{pull_request.number} because its head no longer matches "
        t"{ui.bookmark(expected_head_label)} for "
        t"{ui.change_id(landed_revision.change_id)} at commit "
        t"{ui.commit_id(landed_revision.commit_id)}.",
        hint=t"Run {ui.cmd('view --fetch')} and inspect the review before retrying.",
    )


async def _delete_landed_stack_comments(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
) -> None:
    if cached_change is None:
        return
    comment_targets: tuple[tuple[int | None, StackCommentKind], ...] = (
        (cached_change.navigation_comment_id, "navigation"),
        (cached_change.overview_comment_id, "overview"),
    )
    for comment_id, kind in comment_targets:
        if comment_id is None:
            continue
        await delete_stack_comment(
            comment_id=comment_id,
            github_client=github_client,
            kind=kind,
        )
