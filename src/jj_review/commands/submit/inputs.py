"""Load local submit state and run preflight checks before any GitHub mutation."""

from __future__ import annotations

from collections.abc import Callable

from jj_review import ui
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.github.resolution import select_submit_remote
from jj_review.jj import JjClient
from jj_review.models.stack import LocalRevision
from jj_review.review.bookmarks import (
    BookmarkResolver,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
    match_bookmarks_for_revisions,
)
from jj_review.state.store import ReviewStateStore

from .descriptions import resolve_generated_descriptions
from .intents import repair_interrupted_untracked_remote_bookmarks
from .models import PreparedSubmitInputs, PrivateCommitFinder


def prepare_submit_inputs(
    *,
    config: RepoConfig,
    describe_with: str | None,
    dry_run: bool,
    jj_client: JjClient,
    on_prepared: Callable[[str, str], None] | None,
    revset: str | None,
    state_store: ReviewStateStore,
    use_bookmarks: tuple[str, ...],
) -> PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = jj_client
    remote = select_submit_remote(client.list_git_remotes())
    if not dry_run:
        repair_interrupted_untracked_remote_bookmarks(
            client=client,
            remote=remote,
            state_dir=state_store.require_writable(),
        )
    stack = client.discover_review_stack(revset)
    if on_prepared is not None:
        on_prepared(
            stack.head.change_id,
            stack.head.subject,
        )
    state = state_store.load()
    bookmark_states = client.list_bookmark_states()
    matched_bookmarks = match_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        patterns=use_bookmarks,
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
        describe_with=describe_with,
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
