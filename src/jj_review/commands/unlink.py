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
from jj_review.commands._operation_lock import mutating_command_lock
from jj_review.errors import CliError
from jj_review.jj import JjCliArgs, JjClient
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.bookmarks import bookmark_ownership_for_source
from jj_review.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_review.review.selection import resolve_selected_revset
from jj_review.review.status import (
    PreparedRevision,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    stream_status_async,
)
from jj_review.state.store import ReviewStateStore

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


@dataclass(frozen=True, slots=True)
class _PreparedUnlink:
    """Resolved unlink target after local and GitHub inspection."""

    bookmark: str | None
    cached_change: CachedChange | None
    prepared_client: JjClient
    prepared_revision: PreparedRevision
    review_status: ReviewChangeStatus
    selected_revset: str
    state: ReviewState
    state_store: ReviewStateStore
    status_revision: ReviewStatusRevision


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
    with mutating_command_lock(command="unlink", context=context):
        result = asyncio.run(
            _run_unlink_async(
                context=context,
                options=_unlink_options_from_cli(revset=revset),
            )
        )
    _print_unlink_result(result)
    return 0


def _unlink_options_from_cli(*, revset: str | None) -> UnlinkOptions:
    return UnlinkOptions(revset=revset)


def _print_unlink_result(result: UnlinkResult) -> None:
    revision_label = t"{result.subject} ({ui.change_id(result.change_id)})"
    if result.already_unlinked:
        console.output(t"{revision_label} is already unlinked from review tracking.")
        return
    if result.bookmark is None:
        console.output(t"Stopped review tracking for {revision_label}.")
    else:
        console.output(
            t"Stopped review tracking for {revision_label}, preserving "
            t"{ui.bookmark(result.bookmark)}."
        )


async def _run_unlink_async(
    *,
    context: CommandContext,
    options: UnlinkOptions,
) -> UnlinkResult:
    prepared_unlink = await _prepare_unlink(context=context, options=options)
    if prepared_unlink.review_status.link == "unlinked":
        return _unlink_result(
            already_unlinked=True,
            prepared_unlink=prepared_unlink,
        )

    if not _revision_has_active_review_link(
        bookmark=prepared_unlink.bookmark,
        cached_change=prepared_unlink.cached_change,
        prepared_client=prepared_unlink.prepared_client,
        prepared_revision=prepared_unlink.prepared_revision,
        review_status=prepared_unlink.review_status,
    ):
        raise CliError(
            t"The selected change has no active review tracking link to unlink.",
            hint=(
                t"Use {ui.cmd('relink')} only when you need to attach an existing PR "
                t"intentionally."
            ),
        )

    _apply_unlink(prepared_unlink=prepared_unlink)
    return _unlink_result(
        already_unlinked=False,
        prepared_unlink=prepared_unlink,
    )


async def _prepare_unlink(
    *,
    context: CommandContext,
    options: UnlinkOptions,
) -> _PreparedUnlink:
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
    return _PreparedUnlink(
        bookmark=bookmark,
        cached_change=cached_change,
        prepared_client=prepared.client,
        prepared_revision=prepared_revision,
        review_status=classify_review_status_revision(status_revision),
        selected_revset=prepared_status.selected_revset,
        state=state,
        state_store=state_store,
        status_revision=status_revision,
    )


def _apply_unlink(*, prepared_unlink: _PreparedUnlink) -> None:
    cached_change = prepared_unlink.cached_change
    prepared_revision = prepared_unlink.prepared_revision
    status_revision = prepared_unlink.status_revision
    updated_change = (
        cached_change or CachedChange(bookmark=prepared_unlink.bookmark)
    ).model_copy(
        update={
            "bookmark": prepared_unlink.bookmark,
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
    next_state = prepared_unlink.state.model_copy(
        update={
            "changes": {
                **prepared_unlink.state.changes,
                prepared_revision.revision.change_id: updated_change,
            }
        }
    )
    prepared_unlink.state_store.save(next_state)


def _unlink_result(
    *,
    already_unlinked: bool,
    prepared_unlink: _PreparedUnlink,
) -> UnlinkResult:
    revision = prepared_unlink.prepared_revision.revision
    return UnlinkResult(
        already_unlinked=already_unlinked,
        bookmark=prepared_unlink.bookmark,
        change_id=revision.change_id,
        selected_revset=prepared_unlink.selected_revset,
        subject=revision.subject,
    )


def _resolved_unlink_bookmark(
    *,
    cached_change: CachedChange | None,
    prepared_revision: PreparedRevision,
    status_revision: ReviewStatusRevision,
) -> str | None:
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
    cached_change: CachedChange | None,
    prepared_client: JjClient,
    prepared_revision: PreparedRevision,
    review_status: ReviewChangeStatus,
) -> bool:
    cached_status = classify_saved_review_change(cached_change, local="present")
    if cached_status.link == "active" and cached_status.saved_review_identity:
        return True
    if bookmark is not None:
        bookmark_state = prepared_client.get_bookmark_state(bookmark)
        if bookmark_state.local_target == prepared_revision.revision.commit_id:
            return True
    if review_status.remote_branch != "absent":
        return True
    return review_status.pr_lifecycle in {"open", "closed", "merged"}


def _status_revision_for_change(
    *,
    status_result: StatusResult,
    change_id: str,
) -> ReviewStatusRevision:
    for revision in status_result.revisions:
        if revision.change_id == change_id:
            return revision
    raise AssertionError("Selected unlink change is missing from the status result.")
