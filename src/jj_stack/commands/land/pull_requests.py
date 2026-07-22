"""Merge one landable pull request through the GitHub API."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubPullRequest
from jj_stack.review.landed import (
    delete_landed_stack_comments,
    landed_pull_request_head_mismatch,
)

from .models import LandAction, LandRevision


async def merge_landed_pull_request(
    *,
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
                t"Could not retarget PR #{pull_request.number} to {ui.bookmark(trunk_branch)}"
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
            raise CliError(t"Could not merge PR #{pull_request.number} on GitHub") from error
        try:
            pull_request = await github_client.get_pull_request(
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            raise CliError(t"Could not reload PR #{pull_request.number} after merging") from error
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
    await delete_landed_stack_comments(
        github_client=github_client,
        pull_request_number=pull_request.number,
    )
    return pull_request, None


def _ensure_landed_pull_request_head(
    *,
    github_client: GithubClient,
    landed_revision: LandRevision,
    pull_request: GithubPullRequest,
) -> None:
    mismatch = landed_pull_request_head_mismatch(
        bookmark=landed_revision.bookmark,
        commit_id=landed_revision.commit_id,
        github_client=github_client,
        pull_request=pull_request,
    )
    if mismatch is None:
        return
    raise CliError(
        t"Cannot finalize {ui.change_id(landed_revision.change_id)}: {mismatch}.",
        hint=t"Run {ui.cmd('view --fetch')} and inspect the review before retrying.",
    )
