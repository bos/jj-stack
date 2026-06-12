"""Stop tracking one local change with jj-stack while leaving the rest of the
stack alone.

Later jj-stack commands will ignore that change unless you link it again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.review_state import CachedChange
from jj_stack.review.bookmarks import bookmark_ownership_for_source
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_stack.review.selection import resolve_selected_revset
from jj_stack.review.status import (
    PreparedRevision,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    stream_status_async,
)
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Stop managing one local change as part of review"


@dataclass(frozen=True, slots=True)
class UnlinkResult:
    """Rendered unlink result for one selected local revision."""

    already_unlinked: bool
    bookmark: str | None
    change_id: str
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
    with acquire_operation_lock(context.state_store.require_writable(), command="unlink"):
        result = asyncio.run(
            _run_unlink_async(
                context=context,
                revset=revset,
            )
        )
    _print_unlink_result(result)
    return 0


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
    revset: str | None,
) -> UnlinkResult:
    revset = resolve_selected_revset(
        command_label="unlink",
        require_explicit=True,
        revset=revset,
    )
    with console.spinner(description="Inspecting jj stack"):
        prepared_status = prepare_status(
            context=context,
            fetch_remote_state=True,
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
    state = context.state_store.load()
    cached_change = state.changes.get(prepared_revision.revision.change_id)
    bookmark = _resolved_unlink_bookmark(
        cached_change=cached_change,
        prepared_revision=prepared_revision,
        status_revision=status_revision,
    )
    review_status = classify_review_status_revision(status_revision)
    if review_status.link == "unlinked":
        return UnlinkResult(
            already_unlinked=True,
            bookmark=bookmark,
            change_id=prepared_revision.revision.change_id,
            subject=prepared_revision.revision.subject,
        )

    if not _revision_has_active_review_link(
        bookmark=bookmark,
        cached_change=cached_change,
        context=context,
        prepared_revision=prepared_revision,
        review_status=review_status,
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
        }
    ).with_cleared_pr_identity().with_cleared_comments()
    next_state = state.model_copy(
        update={
            "changes": {
                **state.changes,
                prepared_revision.revision.change_id: updated_change,
            }
        }
    )
    context.state_store.save(next_state)
    return UnlinkResult(
        already_unlinked=False,
        bookmark=bookmark,
        change_id=prepared_revision.revision.change_id,
        subject=prepared_revision.revision.subject,
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
    context: CommandContext,
    prepared_revision: PreparedRevision,
    review_status: ReviewChangeStatus,
) -> bool:
    cached_status = classify_saved_review_change(cached_change, local="present")
    if cached_status.link == "active" and cached_status.saved_review_identity:
        return True
    if bookmark is not None:
        bookmark_state = context.jj_client.get_bookmark_state(bookmark)
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
