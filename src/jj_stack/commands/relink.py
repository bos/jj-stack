"""Reconnect a GitHub pull request to a selected local change."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.pr_refs import parse_repo_pr_reference
from jj_stack.github.resolution import require_github_repo, select_submit_remote
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.pr_branch_namespace import current_pr_branch_namespace, pr_branch_matches_change
from jj_stack.stack.pr_facts import duplicate_pr_claim_change_ids
from jj_stack.stack.selected import require_submittable_changes, select_stack_path
from jj_stack.stack.selection import resolve_selected_revset
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Reconnect an existing pull request to a local change"


@dataclass(frozen=True, slots=True)
class RelinkResult:
    """Explicit PR relink result for one local change."""

    branch: str
    change_id: str
    pr_number: int
    subject: str


def relink(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    pr: str,
    repo: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `relink`."""

    context = bootstrap_context(repo=repo, cli_args=cli_args, debug=debug)
    with acquire_operation_lock(context.state_store.require_writable(), command="relink"):
        result = asyncio.run(
            _run_relink_async(
                context=context,
                pr_reference=pr,
                revset=revset,
            )
        )
    console.output(
        t"Relinked PR #{result.pr_number} for {result.subject} "
        t"({ui.change_id(result.change_id)}) -> {ui.bookmark(result.branch)}"
    )
    return 0


async def _run_relink_async(
    *,
    context: CommandContext,
    pr_reference: str,
    revset: str | None,
) -> RelinkResult:
    client = context.jj_client
    state = context.state_store.load()
    selected = resolve_selected_revset(
        command_label="relink",
        require_explicit=True,
        revset=revset,
    )
    stack = select_stack_path(
        jj_client=client,
        revset=selected,
        state=state,
    ).stack
    require_submittable_changes(stack.changes)
    if not stack.changes:
        raise CliError("The selected stack has no changes to link to a pull request.")
    change = stack.head
    remote = select_submit_remote(client.list_git_remotes())
    repo = require_github_repo(remote)
    pr_number = parse_repo_pr_reference(
        reference=pr_reference,
        github_repo=repo,
        invalid_reference_message=(
            f"{pr_reference} is not a pull request number or URL for {repo.full_name}."
        ),
        wrong_repo_message=(f"{pr_reference} does not belong to {repo.full_name}."),
    )
    async with build_github_client(repo=repo) as github_client:
        pr, head_sha = await _load_exact_relink_pr(
            change_id=change.change_id,
            github_client=github_client,
            pr_number=pr_number,
            repo_owner=repo.owner,
        )
    branch = pr.head.ref
    remote_target = client.list_remote_branches(
        remote=remote.name,
        patterns=(f"refs/heads/{branch}",),
    ).get(branch)
    if remote_target is None:
        raise CliError(
            t"Remote branch {ui.bookmark(branch)} for pull request #{pr_number} does not exist."
        )
    if remote_target != head_sha:
        raise CliError(
            t"Pull request #{pr_number} and remote branch {ui.bookmark(branch)} "
            t"no longer identify the same commit."
        )
    if (
        client.read_remote_git_change_id(
            remote=remote.name,
            commit_id=head_sha,
        )
        != change.change_id
    ):
        raise CliError(
            t"Remote branch {ui.bookmark(branch)} does not contain change "
            t"{ui.change_id(change.change_id)}."
        )
    identity = PRIdentity(
        repo_owner=repo.owner,
        repo_name=repo.repo,
        pr_number=pr_number,
        head_owner=repo.owner,
        head_ref=branch,
    )
    _ensure_relinkable_cached_link(
        change_id=change.change_id,
        identity=identity,
        state=state,
    )
    context.state_store.relink_pr(
        change.change_id,
        identity=identity,
        baseline=SubmittedBaseline(commit_id=head_sha),
    )
    return RelinkResult(
        branch=branch,
        change_id=change.change_id,
        pr_number=pr_number,
        subject=change.subject,
    )


async def _load_exact_relink_pr(
    *,
    change_id: str,
    github_client: GithubClient,
    pr_number: int,
    repo_owner: str,
) -> tuple[GithubPR, str]:
    try:
        pr = await github_client.get_pr(pr_number=pr_number)
        head_matches = (
            await github_client.get_prs_by_head_refs(
                head_refs=(pr.head.ref,),
            )
        ).get(pr.head.ref, ())
    except GithubClientError as error:
        raise CliError(f"Could not load pull request #{pr_number}") from error
    if pr.state != "open":
        raise CliError(
            f"Pull request #{pr_number} is not open; cannot relink {pr.state} PRs.",
            hint=t"Reopen it on GitHub to keep reviewing it, or drop the stale tracking with "
            t"{ui.cmd('jj-stack unstack --local')} and submit again.",
        )
    branch = pr.head.ref
    if pr.head.label != f"{repo_owner}:{branch}":
        raise CliError(
            t"Pull request #{pr_number} head "
            t"{ui.bookmark(pr.head.label or branch)} does not belong to the "
            t"configured repo."
        )
    if len(head_matches) != 1 or head_matches[0].number != pr_number:
        raise CliError(
            t"Head branch {ui.bookmark(branch)} does not uniquely identify PR #{pr_number}."
        )
    namespace = current_pr_branch_namespace()
    if not namespace.contains(branch) or not pr_branch_matches_change(
        branch,
        change_id,
    ):
        raise CliError(
            t"Pull request #{pr_number} head {ui.bookmark(branch)} does not match "
            t"change {ui.change_id(change_id)} under {ui.bookmark(namespace.branch_glob)}."
        )
    head_sha = pr.head.sha
    if head_sha is None:
        raise CliError(
            t"GitHub did not report a head commit for PR #{pr_number}.",
            hint="Refresh the pull request on GitHub, then retry.",
        )
    return pr, head_sha


def _ensure_relinkable_cached_link(
    *,
    change_id: str,
    identity: PRIdentity,
    state: TrackingState,
) -> None:
    identities = dict(state.pr_identities)
    identities[change_id] = identity
    if change_id in duplicate_pr_claim_change_ids(identities):
        raise CliError(
            t"PR #{identity.pr_number} or branch {ui.bookmark(identity.head_ref)} is already "
            t"linked to another local change.",
            hint=t"Run {ui.cmd('jj-stack list')} to find the claiming change, then drop its "
            t"tracking with {ui.cmd('jj-stack unstack --local')} or clean it up with "
            t"{ui.cmd('jj-stack cleanup')}.",
        )
