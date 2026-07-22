"""Merge one landable pull request through the GitHub API."""

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

from .authority import land_authority_error
from .models import LandAction, LandRevision


async def merge_landed_pull_request(
    *,
    bypass_readiness: bool,
    context: CommandContext,
    github_client: GithubClient,
    landed_revision: LandRevision,
    merge_method: str,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> tuple[GithubPullRequest | None, LandAction | None]:
    """Retarget one landable PR to trunk and merge it through the GitHub API.

    Returns the merged pull request, or a blocking action when GitHub refuses
    the merge (pending checks, conflicts, or repo policy).
    """

    pull_request, blocked = await _fresh_pull_request_authority(
        bypass_readiness=bypass_readiness,
        context=context,
        expected_bases=(landed_revision.base_ref, trunk_branch),
        github_client=github_client,
        landed_revision=landed_revision,
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
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to {ui.bookmark(trunk_branch)}"
            ) from error
        pull_request, blocked = await _fresh_pull_request_authority(
            bypass_readiness=bypass_readiness,
            context=context,
            expected_bases=(trunk_branch,),
            github_client=github_client,
            landed_revision=landed_revision,
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
            landed_revision=landed_revision,
            merge_method=merge_method,
            pull_request=pull_request,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        )
        if blocked is not None:
            return None, blocked
        return pull_request, None
    if pull_request.state != "merged":
        return None, LandAction(
            kind="boundary",
            body=t"at PR #{pull_request.number} for {landed_revision.subject} "
            t"{ui.change_id(landed_revision.change_id)}: the PR is "
            t"{pull_request.state} instead of merged; inspect it on GitHub and "
            t"rerun {ui.cmd('land --via merge')}",
            status="blocked",
        )
    return pull_request, None


async def _request_merge(
    *,
    context: CommandContext,
    github_client: GithubClient,
    landed_revision: LandRevision,
    merge_method: str,
    pull_request: GithubPullRequest,
    remote_name: str,
    trunk_branch: str,
) -> LandAction | None:
    try:
        await github_client.merge_pull_request(
            expected_head_sha=landed_revision.commit_id,
            pull_number=pull_request.number,
            merge_method=merge_method,
        )
    except GithubClientError as error:
        if await _merge_result_reached_trunk(
            context=context,
            github_client=github_client,
            landed_revision=landed_revision,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        ):
            return None
        if error.status_code == 409:
            detail = "GitHub rejected the merge because the PR head changed;"
            retry = f"jj-stack submit {landed_revision.change_id}"
        elif error.status_code == 405:
            detail = t"GitHub reports it is not mergeable (checks, conflicts, or policy); "
            retry = "jj-stack land --via merge"
        else:
            raise CliError(t"Could not merge PR #{pull_request.number} on GitHub") from error
        return LandAction(
            kind="boundary",
            body=t"at PR #{pull_request.number} for {landed_revision.subject} "
            t"{ui.change_id(landed_revision.change_id)}: {detail} "
            t"rerun {ui.cmd(retry)}",
            status="blocked",
        )
    return None


async def _merge_result_reached_trunk(
    *,
    context: CommandContext,
    github_client: GithubClient,
    landed_revision: LandRevision,
    remote_name: str,
    trunk_branch: str,
) -> bool:
    try:
        context.jj_client.fetch_remote(remote=remote_name, branches=(trunk_branch,))
        trunk_commit_id = context.jj_client.resolve_revision("trunk()").commit_id
    except CliError, GithubClientError, JjCommandError:
        return False
    candidate = LandedReviewCandidate(
        change_id=landed_revision.change_id,
        review_identity=landed_revision.identity,
        submitted_baseline=SubmittedBaseline(commit_id=landed_revision.commit_id),
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
    if (pull_request := observation.reviews[landed_revision.change_id].pull_request) is None:
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
    bypass_readiness: bool,
    context: CommandContext,
    expected_bases: tuple[str, ...],
    github_client: GithubClient,
    landed_revision: LandRevision,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> tuple[GithubPullRequest | None, LandAction | None]:
    try:
        observation = await observe_review_mutation(
            change_ids=(landed_revision.change_id,),
            context=context,
            github_client=github_client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        )
    except (CliError, GithubClientError, JjCommandError) as error:
        return None, _freshness_boundary(landed_revision, str(error))
    error = land_authority_error(
        bypass_readiness=bypass_readiness,
        expected_bases={landed_revision.change_id: expected_bases},
        expected_repository=github_client.repository,
        expected_trunk_branch=trunk_branch,
        expected_trunk_commit_id=trunk_commit_id,
        observation=observation,
        remote_name=remote_name,
        revisions=(landed_revision,),
    )
    if error is not None:
        return None, _freshness_boundary(landed_revision, error)
    pull_request = observation.reviews[landed_revision.change_id].pull_request
    if pull_request is None:
        raise AssertionError("Authorized merge requires a live pull request.")
    return pull_request.normalize_state(), None


def _freshness_boundary(revision: LandRevision, reason: str) -> LandAction:
    return LandAction(
        kind="boundary",
        body=t"at PR #{revision.identity.pr_number} for {revision.subject} "
        t"{ui.change_id(revision.change_id)}: {reason}; inspect it and rerun "
        t"{ui.cmd('land --via merge')}",
        status="blocked",
    )
