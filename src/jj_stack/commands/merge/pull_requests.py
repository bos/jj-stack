"""Merge one reviewed pull request through GitHub."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import SubmittedBaseline
from jj_stack.review.landed import FinalizationContext, observe_landed_candidate
from jj_stack.review.landed_evidence import LandedReviewCandidate, collect_landed_evidence
from jj_stack.review.observation import observe_reviews
from jj_stack.ui import Message

from .models import MergeAction, MergeRevision
from .preconditions import explain_precondition, merge_precondition_error


async def merge_pull_request(
    *,
    context: CommandContext,
    github_client: GithubClient,
    revision: MergeRevision,
    merge_method: str,
    remote_name: str,
    stack_selector: str,
    trunk_branch: str,
) -> tuple[GithubPullRequest | None, MergeAction | None]:
    pull_request, reason = await _fresh(
        bases=(revision.base_ref, trunk_branch),
        context=context,
        github=github_client,
        remote_name=remote_name,
        revision=revision,
        trunk_branch=trunk_branch,
    )
    if reason or pull_request is None:
        return None, _boundary(
            revision,
            explain_precondition(
                reason or "pull request state is unavailable",
                change_id=revision.change_id,
                sync_target=stack_selector,
            ),
        )
    if pull_request.base.ref != trunk_branch:
        try:
            await github_client.update_pull_request(
                pull_number=pull_request.number,
                base=trunk_branch,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to {ui.bookmark(trunk_branch)}",
                hint="Resolve the GitHub error above, then rerun merge.",
            ) from error
        pull_request, reason = await _fresh(
            bases=(trunk_branch,),
            context=context,
            github=github_client,
            remote_name=remote_name,
            revision=revision,
            trunk_branch=trunk_branch,
        )
        if reason or pull_request is None:
            return None, _boundary(
                revision,
                explain_precondition(
                    reason or "retargeted PR state is unavailable",
                    change_id=revision.change_id,
                    sync_target=stack_selector,
                ),
            )
    try:
        await github_client.merge_pull_request(
            expected_head_sha=revision.commit_id,
            pull_number=pull_request.number,
            merge_method=merge_method,
        )
    except GithubClientError as error:
        if await _landed(
            context=context,
            github=github_client,
            remote_name=remote_name,
            revision=revision,
            trunk_branch=trunk_branch,
        ):
            return pull_request, None
        submit = ui.cmd(f"jj-stack submit {short_change_id(revision.change_id)}")
        if error.status_code == 409:
            rejection: Message = t"the PR head changed on GitHub; run {submit} and merge again"
        elif error.status_code == 405:
            rejection = (
                t"GitHub will not merge it: {error.github_message()}; if it conflicts with "
                t"{ui.bookmark(trunk_branch)}, rebase onto {ui.revset('trunk()')}, resolve the "
                t"conflict, and run {submit} before merging again; if a check or repository rule "
                t"is failing, fix that on GitHub first"
            )
        else:
            raise CliError(
                t"Could not merge PR #{pull_request.number} on GitHub",
                hint="Resolve the GitHub error above, then rerun merge.",
            ) from error
        return None, _boundary(revision, rejection)
    return pull_request, None


async def _fresh(
    *,
    bases: tuple[str, ...],
    context: CommandContext,
    github: GithubClient,
    remote_name: str,
    revision: MergeRevision,
    trunk_branch: str,
) -> tuple[GithubPullRequest | None, str | None]:
    try:
        observation = await observe_reviews(
            change_ids=(revision.change_id,),
            context=context,
            github_client=github,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        )
    except (CliError, GithubClientError, JjCommandError) as error:
        return None, str(error)
    error = merge_precondition_error(
        expected_bases={revision.change_id: bases},
        expected_repository=github.repository,
        expected_trunk_branch=trunk_branch,
        observation=observation,
        remote_name=remote_name,
        revisions=(revision,),
    )
    pull_request = observation.reviews[revision.change_id].pull_request
    return (
        pull_request.normalize_state() if pull_request is not None else None,
        error,
    )


async def _landed(
    *,
    context: CommandContext,
    github: GithubClient,
    remote_name: str,
    revision: MergeRevision,
    trunk_branch: str,
) -> bool:
    try:
        context.jj_client.fetch_remote(
            remote=remote_name,
            on_isolation_change=report_fetch_isolation,
        )
        trunk_commit_id = context.jj_client.resolve_revision("trunk()").commit_id
    except CliError, GithubClientError, JjCommandError:
        return False
    candidate = LandedReviewCandidate(
        change_id=revision.change_id,
        review_identity=revision.identity,
        submitted_baseline=SubmittedBaseline(commit_id=revision.commit_id),
    )
    observation, _reason = await observe_landed_candidate(
        candidate,
        FinalizationContext(
            command=context,
            dry_run=False,
            github=github,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk_commit_id,
        ),
    )
    pull_request = (
        observation.reviews[revision.change_id].pull_request if observation is not None else None
    )
    if pull_request is None:
        return False
    exact, rewritten = collect_landed_evidence(
        candidate=candidate,
        context=context,
        pull_request=pull_request,
        repository=github.repository,
        trunk_commit_id=trunk_commit_id,
    )
    return exact.on_trunk or rewritten.on_trunk


def _boundary(revision: MergeRevision, reason: Message) -> MergeAction:
    return MergeAction(
        kind="boundary",
        body=t"at PR #{revision.identity.pr_number} for {revision.subject} "
        t"{ui.change_id(revision.change_id)}: {reason}",
        status="blocked",
    )
