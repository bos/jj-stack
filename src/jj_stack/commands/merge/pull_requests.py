"""Merge one reviewed pull request through the GitHub API."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import SubmittedBaseline
from jj_stack.review.landed import FinalizationContext, observe_landed_candidate
from jj_stack.review.landed_evidence import (
    LandedReviewCandidate,
    collect_landed_evidence,
)
from jj_stack.review.observation import observe_review_mutation

from .authority import merge_authority_error
from .models import MergeAction, MergeRevision


async def merge_pull_request(
    *,
    context: CommandContext,
    github_client: GithubClient,
    revision: MergeRevision,
    merge_method: str,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> tuple[GithubPullRequest | None, MergeAction | None]:
    """Retarget one selected PR to trunk and merge it through the GitHub API.

    Returns the merged pull request, or a blocking action when GitHub refuses
    the merge (pending checks, conflicts, or repo policy).
    """

    pull_request, blocked = await _fresh_pull_request_authority(
        context=context,
        expected_bases=(revision.base_ref, trunk_branch),
        github_client=github_client,
        revision=revision,
        remote_name=remote_name,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )
    if blocked is not None or pull_request is None:
        return None, blocked
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                pull_number=pull_request.number,
                base=trunk_branch,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to {ui.bookmark(trunk_branch)}"
            ) from error
        pull_request, blocked = await _fresh_pull_request_authority(
            context=context,
            expected_bases=(trunk_branch,),
            github_client=github_client,
            revision=revision,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk_commit_id,
        )
        if blocked is not None or pull_request is None:
            return None, blocked
    if pull_request.state == "open":
        blocked = await _request_merge(
            context=context,
            github_client=github_client,
            revision=revision,
            merge_method=merge_method,
            pull_request=pull_request,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        )
        if blocked is not None:
            return None, blocked
        return pull_request, None
    if pull_request.state != "merged":
        return None, MergeAction(
            kind="boundary",
            body=t"at PR #{pull_request.number} for {revision.subject} "
            t"{ui.change_id(revision.change_id)}: the PR is "
            t"{pull_request.state} instead of merged; inspect it on GitHub and "
            t"rerun {ui.cmd('merge')}",
            status="blocked",
        )
    return pull_request, None


async def _request_merge(
    *,
    context: CommandContext,
    github_client: GithubClient,
    revision: MergeRevision,
    merge_method: str,
    pull_request: GithubPullRequest,
    remote_name: str,
    trunk_branch: str,
) -> MergeAction | None:
    try:
        await github_client.merge_pull_request(
            expected_head_sha=revision.commit_id,
            pull_number=pull_request.number,
            merge_method=merge_method,
        )
    except GithubClientError as error:
        if await _merge_result_reached_trunk(
            context=context,
            github_client=github_client,
            revision=revision,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        ):
            return None
        if error.status_code == 409:
            detail = "GitHub rejected the merge because the PR head changed;"
            retry = f"jj-stack submit {revision.change_id}"
        elif error.status_code == 405:
            detail = t"GitHub reports it is not mergeable (checks, conflicts, or policy); "
            retry = "jj-stack merge"
        else:
            raise CliError(t"Could not merge PR #{pull_request.number} on GitHub") from error
        return MergeAction(
            kind="boundary",
            body=t"at PR #{pull_request.number} for {revision.subject} "
            t"{ui.change_id(revision.change_id)}: {detail} "
            t"rerun {ui.cmd(retry)}",
            status="blocked",
        )
    return None


async def _merge_result_reached_trunk(
    *,
    context: CommandContext,
    github_client: GithubClient,
    revision: MergeRevision,
    remote_name: str,
    trunk_branch: str,
) -> bool:
    try:
        context.jj_client.fetch_remote(remote=remote_name, branches=(trunk_branch,))
        trunk_commit_id = context.jj_client.resolve_revision("trunk()").commit_id
    except CliError, GithubClientError, JjCommandError:
        return False
    candidate = LandedReviewCandidate(
        change_id=revision.change_id,
        review_identity=revision.identity,
        submitted_baseline=SubmittedBaseline(commit_id=revision.commit_id),
    )
    observation, _ = await observe_landed_candidate(
        candidate,
        FinalizationContext(
            command=context,
            dry_run=False,
            github=github_client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk_commit_id,
        ),
    )
    if observation is None:
        return False
    if (pull_request := observation.reviews[revision.change_id].pull_request) is None:
        return False
    exact, rewritten = collect_landed_evidence(
        candidate=candidate,
        context=context,
        pull_request=pull_request,
        repository=github_client.repository,
        trunk_commit_id=trunk_commit_id,
    )
    return exact.state == "landed" or rewritten.state == "landed"


async def _fresh_pull_request_authority(
    *,
    context: CommandContext,
    expected_bases: tuple[str, ...],
    github_client: GithubClient,
    revision: MergeRevision,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> tuple[GithubPullRequest | None, MergeAction | None]:
    try:
        observation = await observe_review_mutation(
            change_ids=(revision.change_id,),
            context=context,
            github_client=github_client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        )
    except (CliError, GithubClientError, JjCommandError) as error:
        return None, _freshness_boundary(revision, str(error))
    error = merge_authority_error(
        expected_bases={revision.change_id: expected_bases},
        expected_repository=github_client.repository,
        expected_trunk_branch=trunk_branch,
        expected_trunk_commit_id=trunk_commit_id,
        observation=observation,
        remote_name=remote_name,
        revisions=(revision,),
    )
    if error is not None:
        return None, _freshness_boundary(revision, error)
    pull_request = observation.reviews[revision.change_id].pull_request
    if pull_request is None:
        raise AssertionError("Authorized merge requires a live pull request.")
    return pull_request.normalize_state(), None


def _freshness_boundary(revision: MergeRevision, reason: str) -> MergeAction:
    return MergeAction(
        kind="boundary",
        body=t"at PR #{revision.identity.pr_number} for {revision.subject} "
        t"{ui.change_id(revision.change_id)}: {reason}; inspect it and rerun "
        t"{ui.cmd('merge')}",
        status="blocked",
    )
