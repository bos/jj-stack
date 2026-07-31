"""Reconnect one exact GitHub pull request to a selected local change."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.pull_request_refs import parse_repository_pull_request_reference
from jj_stack.github.resolution import require_github_repo, select_submit_remote
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.review.branches import (
    is_review_branch,
    review_branch_glob,
    review_branch_matches_change,
)
from jj_stack.review.observation import duplicate_review_claim_change_ids
from jj_stack.review.selected import require_reviewable_revisions, select_review_path
from jj_stack.review.selection import resolve_selected_revset
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Reconnect an existing pull request to a local change"


@dataclass(frozen=True, slots=True)
class RelinkResult:
    """Explicit review relink result for one local revision."""

    branch: str
    change_id: str
    pull_request_number: int
    subject: str


def relink(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    pull_request: str,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `relink`."""

    context = bootstrap_context(repository=repository, cli_args=cli_args, debug=debug)
    with acquire_operation_lock(context.state_store.require_writable(), command="relink"):
        result = asyncio.run(
            _run_relink_async(
                context=context,
                pull_request_reference=pull_request,
                revset=revset,
            )
        )
    console.output(
        t"Relinked PR #{result.pull_request_number} for {result.subject} "
        t"({ui.change_id(result.change_id)}) -> {ui.bookmark(result.branch)}"
    )
    return 0


async def _run_relink_async(
    *,
    context: CommandContext,
    pull_request_reference: str,
    revset: str | None,
) -> RelinkResult:
    client = context.jj_client
    selected = resolve_selected_revset(
        command_label="relink",
        require_explicit=True,
        revset=revset,
    )
    stack = select_review_path(
        jj_client=client,
        revset=selected,
        state=context.state_store.load(),
    ).stack
    require_reviewable_revisions(stack.revisions)
    if not stack.revisions:
        raise CliError("The selected stack has no changes to review.")
    revision = stack.head
    remote = select_submit_remote(client.list_git_remotes())
    repository = require_github_repo(remote)
    client.ensure_review_fetch_isolation(
        remote=remote.name,
        on_change=report_fetch_isolation,
    )
    pull_number = parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=repository,
        invalid_reference_message=(
            f"{pull_request_reference} is not a pull request number or URL for "
            f"{repository.full_name}."
        ),
        wrong_repository_message=(
            f"{pull_request_reference} does not belong to {repository.full_name}."
        ),
    )
    async with build_github_client(repository=repository) as github_client:
        pull_request, head_sha = await _load_exact_relink_pull_request(
            change_id=revision.change_id,
            github_client=github_client,
            pull_number=pull_number,
            repository_owner=repository.owner,
        )
    branch = pull_request.head.ref
    remote_target = client.list_remote_branches(
        remote=remote.name,
        patterns=(f"refs/heads/{branch}",),
    ).get(branch)
    if remote_target is None:
        raise CliError(
            t"Remote branch {ui.bookmark(branch)} for pull request #{pull_number} does not exist."
        )
    if remote_target != head_sha:
        raise CliError(
            t"Pull request #{pull_number} and remote branch {ui.bookmark(branch)} "
            t"no longer identify the same commit."
        )
    if (
        client.read_remote_git_change_id(
            remote=remote.name,
            commit_id=head_sha,
        )
        != revision.change_id
    ):
        raise CliError(
            t"Remote branch {ui.bookmark(branch)} does not contain change "
            t"{ui.change_id(revision.change_id)}."
        )
    identity = ReviewIdentity(
        repository_owner=repository.owner,
        repository_name=repository.repo,
        pr_number=pull_number,
        head_owner=repository.owner,
        head_ref=branch,
    )
    _ensure_relinkable_cached_link(
        change_id=revision.change_id,
        identity=identity,
        state=context.state_store.load(),
    )
    async with build_github_client(repository=repository) as github_client:
        fresh_pull_request, fresh_head_sha = await _load_exact_relink_pull_request(
            change_id=revision.change_id,
            github_client=github_client,
            pull_number=pull_number,
            repository_owner=repository.owner,
        )
    if (fresh_pull_request.head.ref, fresh_head_sha) != (branch, head_sha):
        raise CliError(t"Pull request #{pull_number} changed while relink was preparing; retry.")
    fresh_remote_target = client.list_remote_branches(
        remote=remote.name,
        patterns=(f"refs/heads/{branch}",),
    ).get(branch)
    if fresh_remote_target != head_sha:
        raise CliError(
            t"Remote branch {ui.bookmark(branch)} changed while relink was preparing; retry."
        )
    state = context.state_store.load()
    _ensure_relinkable_cached_link(
        change_id=revision.change_id,
        identity=identity,
        state=state,
    )
    context.state_store.relink_review(
        revision.change_id,
        expected_identity=state.review_identities.get(revision.change_id),
        expected_baseline=state.submitted_baselines.get(revision.change_id),
        expected_issues=state.issues_for(revision.change_id),
        identity=identity,
        baseline=SubmittedBaseline(commit_id=head_sha),
    )
    return RelinkResult(
        branch=branch,
        change_id=revision.change_id,
        pull_request_number=pull_number,
        subject=revision.subject,
    )


async def _load_exact_relink_pull_request(
    *,
    change_id: str,
    github_client: GithubClient,
    pull_number: int,
    repository_owner: str,
) -> tuple[GithubPullRequest, str]:
    try:
        pull_request = await github_client.get_pull_request(pull_number=pull_number)
        head_matches = (
            await github_client.get_pull_requests_by_head_refs(
                head_refs=(pull_request.head.ref,),
            )
        ).get(pull_request.head.ref, ())
    except GithubClientError as error:
        raise CliError(f"Could not load pull request #{pull_number}") from error
    if pull_request.state != "open":
        raise CliError(
            f"Pull request #{pull_number} is not open; cannot relink {pull_request.state} PRs.",
            hint=t"Reopen it on GitHub to keep reviewing it, or drop the stale tracking with "
            t"{ui.cmd('jj-stack unstack --local')} and submit again.",
        )
    branch = pull_request.head.ref
    if pull_request.head.label != f"{repository_owner}:{branch}":
        raise CliError(
            t"Pull request #{pull_number} head "
            t"{ui.bookmark(pull_request.head.label or branch)} does not belong to the "
            t"configured repository."
        )
    if len(head_matches) != 1 or head_matches[0].number != pull_number:
        raise CliError(
            t"Head branch {ui.bookmark(branch)} does not uniquely identify PR #{pull_number}."
        )
    if not is_review_branch(branch) or not review_branch_matches_change(
        branch,
        change_id,
    ):
        raise CliError(
            t"Pull request #{pull_number} head {ui.bookmark(branch)} does not match "
            t"change {ui.change_id(change_id)} under {ui.bookmark(review_branch_glob())}."
        )
    head_sha = pull_request.head.sha
    if head_sha is None:
        raise CliError(
            t"GitHub did not report a head commit for PR #{pull_number}.",
            hint="Refresh the pull request on GitHub, then retry.",
        )
    return pull_request, head_sha


def _ensure_relinkable_cached_link(
    *,
    change_id: str,
    identity: ReviewIdentity,
    state: ReviewState,
) -> None:
    identities = dict(state.review_identities)
    identities[change_id] = identity
    if change_id in duplicate_review_claim_change_ids(identities):
        raise CliError(
            t"PR #{identity.pr_number} or branch {ui.bookmark(identity.head_ref)} is already "
            t"linked to another local change.",
            hint=t"Run {ui.cmd('jj-stack list')} to find the claiming change, then drop its "
            t"tracking with {ui.cmd('jj-stack unstack --local')} or clean it up with "
            t"{ui.cmd('jj-stack cleanup')}.",
        )
