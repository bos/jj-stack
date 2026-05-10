"""Reconnect an existing GitHub pull request to the selected local change.

Use this to repair a missing or wrong local link between a change and its pull
request.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.commands._operation_lock import mutating_command_lock
from jj_review.errors import CliError
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClientError, build_github_client
from jj_review.github.pull_request_refs import parse_repository_pull_request_reference
from jj_review.github.resolution import (
    require_github_repo,
    select_submit_remote,
)
from jj_review.jj import JjCliArgs
from jj_review.models.intent import RelinkIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.selection import resolve_selected_revset
from jj_review.state.intents import check_same_kind_intent, write_new_intent

HELP = "Reconnect an existing pull request to a local change"


@dataclass(frozen=True, slots=True)
class RelinkOptions:
    """Parsed command options for `relink`."""

    pull_request_reference: str
    revset: str | None


@dataclass(frozen=True, slots=True)
class RelinkResult:
    """Explicit review relink result for one local revision."""

    bookmark: str
    change_id: str
    github_repository: str
    pull_request_number: int
    remote_name: str
    selected_revset: str
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
    with mutating_command_lock(command="relink", context=context):
        result = asyncio.run(
            _run_relink_async(
                context=context,
                options=RelinkOptions(
                    pull_request_reference=pull_request,
                    revset=revset,
                ),
            )
        )
    console.output(
        t"Relinked PR #{result.pull_request_number} for {result.subject} "
        t"({ui.change_id(result.change_id)}) -> {ui.bookmark(result.bookmark)}"
    )
    return 0


async def _run_relink_async(
    *,
    context: CommandContext,
    options: RelinkOptions,
) -> RelinkResult:
    client = context.jj_client
    state_store = context.state_store
    state_dir = state_store.require_writable()
    revset = resolve_selected_revset(
        command_label="relink",
        require_explicit=True,
        revset=options.revset,
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
    pull_request_number = parse_repository_pull_request_reference(
        reference=options.pull_request_reference,
        github_repository=github_repository,
        invalid_reference_message=(
            f"{options.pull_request_reference} is not a pull request number or URL for "
            f"{github_repository.full_name}."
        ),
        wrong_host_message=(
            f"{options.pull_request_reference} is not a pull request number or URL for "
            f"{github_repository.full_name}."
        ),
        wrong_repository_message=(
            f"{options.pull_request_reference} does not belong to "
            f"{github_repository.full_name}."
        ),
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
    if (
        bookmark_state.local_target is not None
        and bookmark_state.local_target != revision.commit_id
    ):
        raise CliError(
            t"Local bookmark {ui.bookmark(bookmark)} already points to a different revision.",
            hint="Move or forget it explicitly before relinking.",
        )
    remote_state = bookmark_state.remote_target(remote.name)
    if remote_state is None or not remote_state.targets:
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote.name}')} does not exist.",
            hint=(
                "Fetch and retry once the PR head branch is visible on the selected remote."
            ),
        )
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote.name}')} is conflicted.",
            hint="Resolve it before relinking.",
        )

    state = state_store.load()
    _ensure_relinkable_cached_link(
        bookmark=bookmark,
        change_id=revision.change_id,
        pull_request_number=pull_request.number,
        state=state,
    )

    intent = RelinkIntent(
        kind="relink",
        pid=os.getpid(),
        label=f"relink for {short_change_id(revision.change_id)}",
        change_id=revision.change_id,
        started_at=datetime.now(UTC).isoformat(),
    )
    stale_intents = check_same_kind_intent(state_dir, intent)
    for loaded in stale_intents:
        console.warning(f"A previous relink was interrupted ({loaded.intent.label})")
    intent_path = write_new_intent(state_dir, intent)

    relink_succeeded = False
    try:
        client.set_bookmark(bookmark, revision.change_id)

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
                "navigation_comment_id": None,
                "overview_comment_id": None,
            }
        )
        state_store.save(
            state.model_copy(
                update={
                    "changes": {
                        **state.changes,
                        revision.change_id: updated_change,
                    }
                }
            )
        )
        relink_succeeded = True
        return RelinkResult(
            bookmark=bookmark,
            change_id=revision.change_id,
            github_repository=github_repository.full_name,
            pull_request_number=pull_request.number,
            remote_name=remote.name,
            selected_revset=selected_revset,
            subject=revision.description,
        )
    finally:
        if relink_succeeded:
            intent_path.unlink(missing_ok=True)


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
        if cached_change.bookmark == bookmark and cached_change.link_state != "unlinked":
            raise CliError(
                t"Bookmark {ui.bookmark(bookmark)} is already linked to "
                t"{ui.change_id(cached_change_id)} in local state."
            )
        if (
            cached_change.pr_number == pull_request_number
            and cached_change.link_state != "unlinked"
        ):
            raise CliError(
                t"PR #{pull_request_number} is already linked to "
                t"{ui.change_id(cached_change_id)} in local state."
            )
