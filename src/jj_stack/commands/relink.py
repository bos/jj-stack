"""Reconnect an existing GitHub pull request to the selected local change.

Use this to repair a missing or wrong local link between a change and its pull
request.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.pull_request_refs import parse_repository_pull_request_reference
from jj_stack.github.resolution import (
    ParsedGithubRepo,
    require_github_repo,
    select_submit_remote,
)
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.change_status import classify_review_change, classify_saved_review_change
from jj_stack.review.selection import resolve_selected_revset
from jj_stack.state.journal import OperationJournal
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Reconnect an existing pull request to a local change"


@dataclass(frozen=True, slots=True)
class RelinkResult:
    """Explicit review relink result for one local revision."""

    bookmark: str
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

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(context.state_store.require_writable(), command="relink"):
        result = asyncio.run(
            _run_relink_async(
                context=context,
                pull_request_reference=pull_request,
                revset=revset,
            )
        )
    _print_relink_result(result)
    return 0


async def _run_relink_async(
    *,
    context: CommandContext,
    pull_request_reference: str,
    revset: str | None,
) -> RelinkResult:
    client = context.jj_client
    state_store = context.state_store
    state_dir = state_store.require_writable()
    revset = resolve_selected_revset(
        command_label="relink",
        require_explicit=True,
        revset=revset,
    )

    with console.spinner(description="Inspecting jj stack"):
        stack = client.discover_review_stack(revset)
        if not stack.revisions:
            raise CliError("The selected stack has no changes to review.")
        revision = stack.head
        selected_revset = stack.selected_revset

        remotes = client.list_git_remotes()
        remote = select_submit_remote(remotes)

    with console.spinner(description="Fetching jj remote"):
        client.fetch_remote(remote=remote.name)
    github_repository = require_github_repo(remote)
    pull_request_number = _parse_relink_pull_request_number(
        github_repository=github_repository,
        pull_request_reference=pull_request_reference,
    )

    with console.spinner(description="Loading pull request"):
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            try:
                pull_request = await github_client.get_pull_request(
                    github_repository.owner,
                    github_repository.repo,
                    pull_number=pull_request_number,
                )
            except GithubClientError as error:
                raise CliError(
                    f"Could not load pull request #{pull_request_number}"
                ) from error

    bookmark = _validated_relink_bookmark(
        client=client,
        github_repository=github_repository,
        pull_request=pull_request,
        remote=remote,
        revision=revision,
    )

    state = state_store.load()
    _ensure_relinkable_cached_link(
        bookmark=bookmark,
        change_id=revision.change_id,
        pull_request_number=pull_request.number,
        state=state,
    )
    journal = OperationJournal.begin(
        state_dir,
        operation="relink",
        options={"pull_request_number": pull_request.number},
        resolved_scope={
            "bookmark": bookmark,
            "change_id": revision.change_id,
            "commit_id": revision.commit_id,
            "pull_request_number": pull_request.number,
            "selected_revset": selected_revset,
        },
    )

    relink_succeeded = False
    try:
        cached_change = state.changes.get(revision.change_id)
        updated_change = (cached_change or CachedChange()).model_copy(
            update={
                "bookmark": bookmark,
                "bookmark_ownership": "external",
                "link_state": "active",
                "pr_number": pull_request.number,
                "pr_review_decision": None,
                "pr_state": pull_request.state,
                "pr_url": pull_request.html_url,
            }
        ).with_cleared_comments()
        next_state = state.model_copy(
            update={
                "changes": {
                    **state.changes,
                    revision.change_id: updated_change,
                }
            }
        )
        journal.append(
            "planned_mutation",
            {
                "change_id": revision.change_id,
                "mutation": "saved_state_update",
            },
        )
        state_store.save(next_state)
        journal.append(
            "saved_state_update",
            {
                "after": updated_change,
                "before": cached_change,
                "change_id": revision.change_id,
            },
        )
        journal.append(
            "planned_mutation",
            {
                "bookmark": bookmark,
                "change_id": revision.change_id,
                "mutation": "set_local_bookmark",
            },
        )
        client.set_bookmark(bookmark, revision.change_id)
        journal.append(
            "mutation_applied",
            {
                "bookmark": bookmark,
                "change_id": revision.change_id,
                "mutation": "set_local_bookmark",
            },
        )
        journal.append("completed", {"change_id": revision.change_id})
        relink_succeeded = True
        return RelinkResult(
            bookmark=bookmark,
            change_id=revision.change_id,
            pull_request_number=pull_request.number,
            subject=revision.description,
        )
    finally:
        if not relink_succeeded:
            console.warning("Relink was interrupted; inspect the operation log before retrying.")


def _parse_relink_pull_request_number(
    *,
    github_repository: ParsedGithubRepo,
    pull_request_reference: str,
) -> int:
    return parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=github_repository,
        invalid_reference_message=(
            f"{pull_request_reference} is not a pull request number or URL for "
            f"{github_repository.full_name}."
        ),
        wrong_host_message=(
            f"{pull_request_reference} is not a pull request number or URL for "
            f"{github_repository.full_name}."
        ),
        wrong_repository_message=(
            f"{pull_request_reference} does not belong to {github_repository.full_name}."
        ),
    )


def _validated_relink_bookmark(
    *,
    client: JjClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
    remote: GitRemote,
    revision: LocalRevision,
) -> str:
    if pull_request.state != "open":
        raise CliError(
            f"Pull request #{pull_request.number} is not open; cannot relink "
            f"{pull_request.state} PRs."
        )

    bookmark = pull_request.head.ref
    expected_head_label = f"{github_repository.owner}:{bookmark}"
    if pull_request.head.label != expected_head_label:
        raise CliError(
            t"Pull request #{pull_request.number} head {ui.bookmark(bookmark)} does not "
            t"belong to {github_repository.full_name}. Relink only supports "
            t"same-repository pull request branches."
        )

    bookmark_state = client.get_bookmark_state(bookmark)
    if len(bookmark_state.local_targets) > 1:
        raise CliError(
            t"Local bookmark {ui.bookmark(bookmark)} is conflicted.",
            hint="Resolve it before relinking.",
        )
    if bookmark_state.local_target not in (None, revision.commit_id):
        raise CliError(
            t"Local bookmark {ui.bookmark(bookmark)} already points to a different revision.",
            hint="Move or forget it explicitly before relinking.",
        )
    remote_state = bookmark_state.remote_target(remote.name)
    review_status = classify_review_change(
        cached_change=None,
        commit_id=revision.commit_id,
        local="present",
        pull_request_lookup=None,
        remote_state=remote_state,
    )
    if review_status.remote_branch == "absent":
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote.name}')} does not exist.",
            hint=(
                "Fetch and retry once the PR head branch is visible on the selected remote."
            ),
        )
    if review_status.remote_branch == "conflicted":
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote.name}')} is conflicted.",
            hint="Resolve it before relinking.",
        )
    return bookmark


def _print_relink_result(result: RelinkResult) -> None:
    console.output(
        t"Relinked PR #{result.pull_request_number} for {result.subject} "
        t"({ui.change_id(result.change_id)}) -> {ui.bookmark(result.bookmark)}"
    )


def _ensure_relinkable_cached_link(
    *,
    bookmark: str,
    change_id: str,
    pull_request_number: int,
    state: ReviewState,
) -> None:
    for cached_change_id, cached_change in state.changes.items():
        if cached_change_id == change_id:
            continue
        review_status = classify_saved_review_change(cached_change, local="present")
        if cached_change.bookmark == bookmark and review_status.link != "unlinked":
            raise CliError(
                t"Bookmark {ui.bookmark(bookmark)} is already linked to "
                t"{ui.change_id(cached_change_id)} in local state."
            )
        if (
            cached_change.pr_number == pull_request_number
            and review_status.link != "unlinked"
        ):
            raise CliError(
                t"PR #{pull_request_number} is already linked to "
                t"{ui.change_id(cached_change_id)} in local state."
            )
