"""Load local submit state and run preflight checks before any GitHub mutation."""

from __future__ import annotations

from collections.abc import Callable

from jj_review import ui
from jj_review.bootstrap import CommandContext
from jj_review.errors import CliError
from jj_review.github.resolution import select_submit_remote
from jj_review.models.stack import LocalRevision
from jj_review.review.bookmarks import (
    BookmarkResolver,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
    match_bookmarks_for_revisions,
)
from jj_review.review.restart import restart_state_for_stack

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
    on_prepared: Callable[[str, str], None] | None,
    options: SubmitOptions,
    resolved_options: ResolvedSubmitOptions,
) -> PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = context.jj_client
    config = context.config
    state_store = context.state_store
    remote = select_submit_remote(client.list_git_remotes())
    stack = client.discover_review_stack(options.revset)
    if on_prepared is not None:
        on_prepared(
            stack.head.change_id,
            stack.head.subject,
        )
    state = state_store.load()
    bookmark_states = client.list_bookmark_states()
    restarted_change_ids: frozenset[str] = frozenset()
    if options.restart:
        restart_result = restart_state_for_stack(
            bookmark_states=bookmark_states,
            config=config,
            stack=stack,
            state=state,
        )
        state = restart_result.state
        restarted_change_ids = frozenset(
            restarted.change_id for restarted in restart_result.changed
        )
    matched_bookmarks = match_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        patterns=resolved_options.use_bookmarks,
        revisions=stack.revisions,
        remote_name=remote.name,
    )
    discovered_bookmarks = discover_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        prefix=config.bookmark_prefix,
        remote_name=remote.name,
        revisions=stack.revisions,
    )
    bookmark_result = BookmarkResolver(
        state,
        prefix=config.bookmark_prefix,
        matched_bookmarks=matched_bookmarks,
        discovered_bookmarks=discovered_bookmarks,
    ).pin_revisions(stack.revisions)
    ensure_unique_bookmarks(bookmark_result.resolutions)
    preflight_conflicted_revisions(stack.revisions)
    preflight_private_commits(client, stack.revisions)
    (
        generated_pull_request_descriptions,
        generated_stack_description,
    ) = resolve_generated_descriptions(
        describe_with=options.describe_with,
        jj_client=client,
        selected_revset=stack.selected_revset,
        revisions=stack.revisions,
    )
    return PreparedSubmitInputs(
        bookmark_states=bookmark_states,
        bookmark_result=bookmark_result,
        client=client,
        generated_pull_request_descriptions=generated_pull_request_descriptions,
        generated_stack_description=generated_stack_description,
        remote=remote,
        restarted_change_ids=restarted_change_ids,
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
        t"Stack contains commits blocked by "
        t"{ui.code('git.private-commits')}: {subjects}.",
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
    raise CliError(
        t"Stack contains changes with unresolved conflicts: {subjects}. "
        t"Resolve these changes before submitting."
    )
