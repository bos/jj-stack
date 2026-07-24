"""Load local submit state and run preflight checks before any GitHub mutation."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ConflictedStackError
from jj_stack.github.resolution import select_submit_remote
from jj_stack.models.stack import LocalRevision
from jj_stack.review.bookmarks import (
    BookmarkResolver,
    ResolvedBookmark,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
)
from jj_stack.review.restart import RestartedReview, restart_state_for_stack

from .descriptions import resolve_generated_descriptions
from .models import (
    PreparedSubmitInputs,
    PrivateCommitFinder,
    ResolvedSubmitOptions,
    SubmitOptions,
)


def prepare_submit_inputs(
    *,
    context: CommandContext,
    options: SubmitOptions,
    resolved_options: ResolvedSubmitOptions,
) -> PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = context.jj_client
    state_store = context.state_store
    remote = select_submit_remote(client.list_git_remotes())
    stack = client.discover_review_stack(options.revset)
    state = state_store.load()
    for revision in stack.revisions:
        if state.issues_for(revision.change_id):
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is malformed.",
                hint=t"Repair it with {ui.cmd('relink')} before submitting the review.",
            )
    bookmark_states = client.list_bookmark_states()
    restarted_change_ids: frozenset[str] = frozenset()
    restarted_reviews: tuple[RestartedReview, ...] = ()
    forced_bookmarks: dict[str, str] = {}
    if options.restart:
        restart_result = restart_state_for_stack(
            bookmark_states=bookmark_states,
            remote_name=remote.name,
            stack=stack,
            state=state,
        )
        state = restart_result.state
        restarted_reviews = restart_result.restarted
        restarted_change_ids = frozenset(
            restarted.change_id for restarted in restart_result.changed
        )
        forced_bookmarks = {
            restarted.change_id: restarted.new_bookmark for restarted in restart_result.changed
        }
    discovered_bookmarks = discover_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        remote_name=remote.name,
        revisions=tuple(
            revision
            for revision in stack.revisions
            if revision.change_id not in restarted_change_ids
        ),
    )
    bookmark_resolutions = BookmarkResolver(
        state.review_identities,
        discovered_bookmarks=discovered_bookmarks,
    ).resolve_revisions(stack.revisions)
    if forced_bookmarks:
        bookmark_resolutions = tuple(
            ResolvedBookmark(
                bookmark=forced_bookmarks[resolution.change_id],
                change_id=resolution.change_id,
                source="generated",
            )
            if resolution.change_id in forced_bookmarks
            else resolution
            for resolution in bookmark_resolutions
        )
    ensure_unique_bookmarks(bookmark_resolutions)
    preflight_conflicted_revisions(stack.revisions)
    preflight_private_commits(client, stack.revisions)
    (
        generated_pull_request_descriptions,
        generated_stack_description,
    ) = resolve_generated_descriptions(
        descriptions=options.descriptions,
        describe_with=options.describe_with,
        edit=options.edit,
        jj_client=client,
        selected_revset=stack.selected_revset,
        revisions=stack.revisions,
    )
    return PreparedSubmitInputs(
        bookmark_states=bookmark_states,
        bookmark_resolutions=bookmark_resolutions,
        client=client,
        generated_pull_request_descriptions=generated_pull_request_descriptions,
        generated_stack_description=generated_stack_description,
        remote=remote,
        restarted_change_ids=restarted_change_ids,
        restarted_reviews=restarted_reviews,
        stack=stack,
        state=state,
    )


def preflight_private_commits(
    client: PrivateCommitFinder,
    revisions: tuple[LocalRevision, ...],
) -> None:
    private = client.find_private_commits(revisions)
    if not private:
        return
    subjects = ui.join(
        lambda revision: t"{ui.change_id(revision.change_id)} ({revision.subject})",
        private,
    )
    raise CliError(
        t"Stack contains commits blocked by {ui.code('git.private-commits')}: {subjects}.",
        hint="Remove these changes from the stack before submitting.",
    )


def preflight_conflicted_revisions(revisions: tuple[LocalRevision, ...]) -> None:
    conflicted = tuple(revision for revision in revisions if revision.conflict)
    if not conflicted:
        return
    subjects = ui.join(
        lambda revision: t"{ui.change_id(revision.change_id)} ({revision.subject})",
        conflicted,
    )
    raise ConflictedStackError(
        t"Stack contains changes with unresolved conflicts: {subjects}. "
        t"Resolve these changes before submitting."
    )
