"""Stop tracking one local change with jj-review while leaving the rest of the
stack alone.

Later jj-review commands will ignore that change unless you link it again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.errors import CliError
from jj_review.jj import JjCliArgs
from jj_review.models.review_state import CachedChange
from jj_review.review.bookmarks import bookmark_ownership_for_source
from jj_review.review.selection import resolve_selected_revset
from jj_review.review.status import (
    prepare_status,
    stream_status_async,
)

HELP = "Stop managing one local change as part of review"


@dataclass(frozen=True, slots=True)
class UnlinkOptions:
    """Parsed command options for `unlink`."""

    revset: str | None


@dataclass(frozen=True, slots=True)
class UnlinkResult:
    """Rendered unlink result for one selected local revision."""

    already_unlinked: bool
    bookmark: str | None
    change_id: str
    selected_revset: str
    subject: str


def unlink(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `unlink`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    result = asyncio.run(
        _run_unlink_async(
            context=context,
            options=UnlinkOptions(revset=revset),
        )
    )
    revision_label = t"{result.subject} ({ui.change_id(result.change_id)})"
    if result.already_unlinked:
        console.output(t"{revision_label} is already unlinked from review tracking.")
        return 0
    if result.bookmark is None:
        console.output(t"Stopped review tracking for {revision_label}.")
    else:
        console.output(
            t"Stopped review tracking for {revision_label}, preserving "
            t"{ui.bookmark(result.bookmark)}."
        )
    return 0


async def _run_unlink_async(
    *,
    context: CommandContext,
    options: UnlinkOptions,
) -> UnlinkResult:
    revset = resolve_selected_revset(
        command_label="unlink",
        require_explicit=True,
        revset=options.revset,
    )
    with console.spinner(description="Inspecting jj stack"):
        prepared_status = prepare_status(
            config=context.config,
            fetch_remote_state=True,
            jj_client=context.jj_client,
            persist_bookmarks=False,
            revset=revset,
        )
    prepared = prepared_status.prepared
    if not prepared.status_revisions:
        raise CliError("The selected stack has no changes to review.")

    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = await stream_status_async(
            persist_cache_updates=False,
            prepared_status=prepared_status,
            on_github_status=None,
            on_revision=lambda _revision, _github_available: progress.advance(),
        )
    prepared_revision = prepared.status_revisions[-1]
    status_revision = _status_revision_for_change(
        status_result=status_result,
        change_id=prepared_revision.revision.change_id,
    )
    state_store = prepared.state_store
    state = state_store.load()
    cached_change = state.changes.get(prepared_revision.revision.change_id)
    bookmark = _resolved_unlink_bookmark(
        cached_change=cached_change,
        prepared_revision=prepared_revision,
        status_revision=status_revision,
    )
    if cached_change is not None and cached_change.is_unlinked:
        return UnlinkResult(
            already_unlinked=True,
            bookmark=bookmark,
            change_id=prepared_revision.revision.change_id,
            selected_revset=prepared_status.selected_revset,
            subject=prepared_revision.revision.subject,
        )

    if not _revision_has_active_review_link(
        bookmark=bookmark,
        cached_change=cached_change,
        prepared_client=prepared.client,
        prepared_revision=prepared_revision,
        status_revision=status_revision,
    ):
        raise CliError(
            t"The selected change has no active review tracking link to unlink.",
            hint=(
                t"Use {ui.cmd('relink')} only when you need to attach an existing PR "
                t"intentionally."
            ),
        )

    updated_change = (cached_change or CachedChange(bookmark=bookmark)).model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": (
                cached_change.bookmark_ownership
                if cached_change is not None
                else bookmark_ownership_for_source(status_revision.bookmark_source)
            ),
            "link_state": "unlinked",
            "pr_number": None,
            "pr_review_decision": None,
            "pr_state": None,
            "pr_url": None,
            "navigation_comment_id": None,
            "overview_comment_id": None,
        }
    )
    next_state = state.model_copy(
        update={
            "changes": {
                **state.changes,
                prepared_revision.revision.change_id: updated_change,
            }
        }
    )
    state_store.save(next_state)
    return UnlinkResult(
        already_unlinked=False,
        bookmark=bookmark,
        change_id=prepared_revision.revision.change_id,
        selected_revset=prepared_status.selected_revset,
        subject=prepared_revision.revision.subject,
    )


def _resolved_unlink_bookmark(*, cached_change, prepared_revision, status_revision) -> str | None:
    if cached_change is not None and cached_change.bookmark is not None:
        return cached_change.bookmark
    pull_request_lookup = status_revision.pull_request_lookup
    if pull_request_lookup is not None and pull_request_lookup.pull_request is not None:
        return pull_request_lookup.pull_request.head.ref
    if prepared_revision.bookmark_source != "generated":
        return prepared_revision.bookmark
    return None


def _revision_has_active_review_link(
    *,
    bookmark: str | None,
    cached_change,
    prepared_client,
    prepared_revision,
    status_revision,
) -> bool:
    if (
        cached_change is not None
        and not cached_change.is_unlinked
        and (
            cached_change.bookmark is not None
            or cached_change.pr_number is not None
            or cached_change.pr_url is not None
            or cached_change.pr_state is not None
            or cached_change.navigation_comment_id is not None
            or cached_change.overview_comment_id is not None
        )
    ):
        return True
    if bookmark is not None:
        bookmark_state = prepared_client.get_bookmark_state(bookmark)
        if bookmark_state.local_target == prepared_revision.revision.commit_id:
            return True
    remote_state = status_revision.remote_state
    if remote_state is not None and remote_state.targets:
        return True
    pull_request_lookup = status_revision.pull_request_lookup
    return pull_request_lookup is not None and pull_request_lookup.pull_request is not None


def _status_revision_for_change(*, status_result, change_id: str):
    for revision in status_result.revisions:
        if revision.change_id == change_id:
            return revision
    raise AssertionError("Selected unlink change is missing from the status result.")
