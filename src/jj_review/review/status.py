"""Review status preparation and GitHub inspection helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from jj_review import ui
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_review.config import RepoConfig
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.formatting import short_change_id
from jj_review.github.client import (
    GithubClient,
    GithubClientError,
    build_github_client,
)
from jj_review.github.error_messages import (
    summarize_github_lookup_error,
)
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    select_submit_remote,
)
from jj_review.github.stack_comments import (
    is_navigation_comment,
    is_overview_comment,
)
from jj_review.jj import JjClient, UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubIssueComment, GithubPullRequest
from jj_review.models.intent import LoadedIntent
from jj_review.models.review_state import CachedChange, LinkState, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.bookmarks import (
    BookmarkResolver,
    BookmarkSource,
    bookmark_ownership_for_source,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
    match_bookmarks_for_revisions,
)
from jj_review.review.change_status import (
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_review.review.intents import intent_is_stale
from jj_review.review.topology import (
    SubmittedStateDisagreement,
    submitted_state_disagreements,
)
from jj_review.state.store import ReviewStateStore
from jj_review.ui import Message

logger = logging.getLogger(__name__)
_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY

HELP = "Check the review status of one or more jj stacks"

PullRequestLookupState = Literal["ambiguous", "closed", "error", "missing", "open"]
PullRequestLookupSource = Literal["head", "remembered"]
ManagedCommentsLookupState = Literal["ambiguous", "error", "resolved"]


@dataclass(frozen=True, slots=True)
class PullRequestLookup:
    """Best-effort GitHub pull request lookup for one branch."""

    message: ErrorMessage | None
    pull_request: GithubPullRequest | None
    state: PullRequestLookupState
    review_decision: str | None = None
    review_decision_error: str | None = None
    repository_error: ErrorMessage | None = None
    source: PullRequestLookupSource = "head"


@dataclass(frozen=True, slots=True)
class ManagedCommentsLookup:
    """Best-effort GitHub managed-comment lookup for one pull request."""

    message: ErrorMessage | None
    navigation_comment: GithubIssueComment | None
    overview_comment: GithubIssueComment | None
    state: ManagedCommentsLookupState


@dataclass(frozen=True, slots=True)
class ReviewStatusRevision:
    """Rendered pull-request and branch state for one local revision."""

    bookmark: str
    bookmark_source: BookmarkSource
    cached_change: CachedChange | None
    change_id: str
    commit_id: str
    link_state: LinkState
    local_divergent: bool
    pull_request_lookup: PullRequestLookup | None
    remote_state: RemoteBookmarkState | None
    managed_comments_lookup: ManagedCommentsLookup | None
    subject: str

    def pull_request(self) -> GithubPullRequest | None:
        lookup = self.pull_request_lookup
        if lookup is None:
            return None
        return lookup.pull_request

    def pull_request_number(self) -> int | None:
        pull_request = self.pull_request()
        if pull_request is None:
            return None
        return pull_request.number

    def pull_request_base_ref(self) -> str | None:
        pull_request = self.pull_request()
        if pull_request is None:
            return None
        return pull_request.base.ref

    def has_merged_pull_request(self) -> bool:
        return classify_review_status_revision(self).pr_lifecycle == "merged"

    def has_closed_unmerged_pull_request(self) -> bool:
        return classify_review_status_revision(self).pr_lifecycle == "closed"


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Status result for one selected local stack."""

    github_error: ErrorMessage | None
    github_repository: str | None
    incomplete: bool
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    revisions: tuple[ReviewStatusRevision, ...]
    selected_revset: str
    base_parent_subject: str
    submitted_state_disagreements: tuple[SubmittedStateDisagreement, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedStatus:
    """Locally prepared status inputs before any GitHub inspection."""

    github_repository: ParsedGithubRepo | None
    github_repository_error: ErrorMessage | None
    outstanding_intents: tuple[LoadedIntent, ...]
    prepared: PreparedStack
    selected_revset: str
    stale_intents: tuple[LoadedIntent, ...]
    base_parent_subject: str

    def github_inspection_count(self, *, discover_remote_review: bool = False) -> int:
        """Return how many selected revisions need live GitHub inspection."""

        if self.github_repository is None:
            return 0
        return sum(
            1
            for prepared_revision in self.prepared.status_revisions
            if _needs_github_inspection(
                prepared_revision,
                discover_remote_review=discover_remote_review,
            )
        )


@dataclass(frozen=True, slots=True)
class PreparedStack:
    """Prepared local stack inputs shared across inspection-driven commands."""

    bookmark_states: dict[str, BookmarkState]
    bookmark_result_changed: bool
    client: JjClient
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    stack: LocalStack
    state: ReviewState
    state_changes: dict[str, CachedChange]
    state_store: ReviewStateStore
    status_revisions: tuple[PreparedRevision, ...]


@dataclass(frozen=True, slots=True)
class PreparedRevision:
    """Local review revision with resolved bookmark and cached state."""

    bookmark: str
    bookmark_source: BookmarkSource
    cached_change: CachedChange | None
    revision: LocalRevision


def status_preparation_cli_error(error: UnsupportedStackError) -> CliError:
    """Translate stack-shape preparation failures into a user-facing CLI error."""

    if error.reason == "trunk_resolved_to_root":
        return CliError(
            "No trunk bookmark is configured for this repo.",
            hint=error.hint,
        )
    if error.reason == "divergent_change" and error.change_id is not None:
        return CliError(
            t"Local history does not form a linear stack. {error}",
            hint=(
                t"Inspect the divergent revisions with {ui.cmd('jj log -r')} "
                t"{ui.revset(f'change_id({error.change_id})')} and reconcile them "
                t"before retrying. This can happen after {ui.cmd('status --fetch')} "
                t"or another fetch imports remote bookmark updates for landed PRs."
            ),
        )
    return CliError(t"Local history does not form a linear stack. {error}")


def prepare_status(
    *,
    config: RepoConfig,
    fetch_remote_state: bool = False,
    fetch_only_when_tracked: bool = False,
    jj_client: JjClient,
    persist_bookmarks: bool = False,
    re_resolve_after_remote_refresh: bool = False,
    revset: str | None,
) -> PreparedStatus:
    """Resolve local status inputs before any GitHub network inspection."""

    state_store = ReviewStateStore.for_repo(jj_client.repo_root)
    state = state_store.load()
    remotes = jj_client.list_git_remotes()
    remote: GitRemote | None = None
    remote_error: ErrorMessage | None = None
    if remotes:
        try:
            remote = select_submit_remote(remotes)
        except CliError as error:
            remote_error = error_message(error)

    stack: LocalStack | None = None
    if (
        remote is not None
        and fetch_remote_state
        and re_resolve_after_remote_refresh
        and not fetch_only_when_tracked
    ):
        jj_client.fetch_remote(remote=remote.name)
    else:
        stack = jj_client.discover_review_stack(
            revset, allow_divergent=True, allow_immutable=True
        )
        if remote is not None and fetch_remote_state:
            should_fetch = not fetch_only_when_tracked or any(
                classify_saved_review_change(
                    state.changes.get(revision.change_id),
                    local="present",
                ).saved_review_identity
                for revision in stack.revisions
            )
            if should_fetch:
                jj_client.fetch_remote(remote=remote.name)
                if re_resolve_after_remote_refresh:
                    stack = None
    if stack is None:
        stack = jj_client.discover_review_stack(
            revset, allow_divergent=True, allow_immutable=True
        )

    prepared = prepare_stack_for_status(
        config=config,
        jj_client=jj_client,
        persist_bookmarks=persist_bookmarks,
        remote=remote,
        remote_error=remote_error,
        stack=stack,
        state=state,
        state_store=state_store,
    )
    logger.debug(
        "status prepared: selected_revset=%s revisions=%d remote=%s",
        prepared.stack.selected_revset,
        len(prepared.status_revisions),
        prepared.remote.name if prepared.remote is not None else "unavailable",
    )
    github_repository = None
    github_repository_error = None
    if prepared.remote is not None:
        github_repository = parse_github_repo(prepared.remote)
        if github_repository is None:
            github_repository_error = (
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(prepared.remote.name)}. Use a GitHub remote URL."
            )
    outstanding_intents, stale_intents = _classify_status_intents(prepared)

    return PreparedStatus(
        github_repository=github_repository,
        github_repository_error=github_repository_error,
        outstanding_intents=outstanding_intents,
        prepared=prepared,
        selected_revset=prepared.stack.selected_revset,
        stale_intents=stale_intents,
        base_parent_subject=prepared.stack.base_parent.subject,
    )


def refresh_remote_state_for_status(*, jj_client: JjClient) -> None:
    """Refresh remembered remote state once for `status --fetch` when possible."""

    remotes = jj_client.list_git_remotes()
    if not remotes:
        return
    try:
        remote = select_submit_remote(remotes)
    except CliError:
        return
    jj_client.fetch_remote(remote=remote.name)


def _classify_status_intents(
    prepared: PreparedStack,
) -> tuple[tuple[LoadedIntent, ...], tuple[LoadedIntent, ...]]:
    outstanding_intents: list[LoadedIntent] = []
    stale_intents: list[LoadedIntent] = []
    now = datetime.now(UTC)

    for loaded in prepared.state_store.list_intents():
        if intent_is_stale(
            loaded.intent,
            lambda change_id: _change_id_resolves(prepared.client, change_id),
            now=now,
        ):
            stale_intents.append(loaded)
        else:
            outstanding_intents.append(loaded)
    return tuple(outstanding_intents), tuple(stale_intents)


def stream_status(
    *,
    discover_remote_review: bool = False,
    inspect_stack_comments: bool = False,
    persist_cache_updates: bool = True,
    prepared_status: PreparedStatus,
    on_github_status: Callable[[str | None, ErrorMessage | None], None] | None = None,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None = None,
) -> StatusResult:
    """Inspect GitHub state for a prepared stack and optionally stream results out."""

    return asyncio.run(
        stream_status_async(
            discover_remote_review=discover_remote_review,
            inspect_stack_comments=inspect_stack_comments,
            on_github_status=on_github_status,
            on_revision=on_revision,
            persist_cache_updates=persist_cache_updates,
            prepared_status=prepared_status,
        )
    )


async def stream_status_async(
    *,
    discover_remote_review: bool = False,
    inspect_stack_comments: bool = False,
    on_github_status: Callable[[str | None, ErrorMessage | None], None] | None,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None,
    persist_cache_updates: bool = True,
    prepared_status: PreparedStatus,
) -> StatusResult:
    prepared = prepared_status.prepared
    selected_revset = prepared_status.selected_revset
    base_parent_subject = prepared_status.base_parent_subject
    github_repository = prepared_status.github_repository
    github_repository_error = prepared_status.github_repository_error
    submitted_disagreements = submitted_state_disagreements(
        prepared.state,
        (prepared.stack,),
    )

    if prepared.remote is None:
        display_revisions = tuple(reversed(build_status_revisions_for_prepared_stack(prepared)))
        if on_github_status is not None:
            on_github_status(None, None)
        for revision in display_revisions:
            if on_revision is not None:
                on_revision(revision, False)
        return StatusResult(
            github_error=None,
            github_repository=None,
            incomplete=True,
            remote=None,
            remote_error=prepared.remote_error,
            revisions=display_revisions,
            selected_revset=selected_revset,
            base_parent_subject=base_parent_subject,
            submitted_state_disagreements=submitted_disagreements,
        )

    if github_repository is None:
        logger.debug("status github target unavailable: %s", github_repository_error)
        display_revisions = tuple(reversed(build_status_revisions_for_prepared_stack(prepared)))
        if on_github_status is not None:
            on_github_status(None, github_repository_error)
        for revision in display_revisions:
            if on_revision is not None:
                on_revision(revision, False)
        return StatusResult(
            github_error=github_repository_error,
            github_repository=None,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            revisions=display_revisions,
            selected_revset=selected_revset,
            base_parent_subject=base_parent_subject,
            submitted_state_disagreements=submitted_disagreements,
        )

    github_status_reported = False

    def emit_github_status(github_error: ErrorMessage | None) -> None:
        nonlocal github_status_reported
        if github_status_reported:
            return
        github_status_reported = True
        if on_github_status is not None:
            on_github_status(github_repository.full_name, github_error)

    if not prepared.status_revisions:
        if on_github_status is not None:
            on_github_status(github_repository.full_name, None)
        return StatusResult(
            github_error=None,
            github_repository=github_repository.full_name,
            incomplete=False,
            remote=prepared.remote,
            remote_error=None,
            revisions=(),
            selected_revset=selected_revset,
            base_parent_subject=base_parent_subject,
            submitted_state_disagreements=submitted_disagreements,
        )

    fallback_revisions = tuple(reversed(build_status_revisions_for_prepared_stack(prepared)))
    prepared_revisions_for_github = tuple(
        prepared_revision
        for prepared_revision in prepared.status_revisions
        if _needs_github_inspection(
            prepared_revision,
            discover_remote_review=discover_remote_review,
        )
    )
    if not prepared_revisions_for_github:
        return StatusResult(
            github_error=None,
            github_repository=github_repository.full_name,
            incomplete=_status_is_incomplete(fallback_revisions),
            remote=prepared.remote,
            remote_error=None,
            revisions=fallback_revisions,
            selected_revset=selected_revset,
            base_parent_subject=base_parent_subject,
            submitted_state_disagreements=submitted_disagreements,
        )

    revisions: list[ReviewStatusRevision] = []
    try:
        async for revision in _iter_status_revisions_with_github(
            github_repository=github_repository,
            inspect_stack_comments=inspect_stack_comments,
            on_github_status=emit_github_status,
            prepared=prepared,
            prepared_revisions=prepared_revisions_for_github,
        ):
            revisions.append(revision)
            if on_revision is not None:
                on_revision(revision, True)
    except CliError as error:
        if not github_status_reported:
            emit_github_status(None)
        github_error = error_message(error)
        logger.debug("status github inspection failed: %s", github_error)
        streamed_change_ids = {revision.change_id for revision in revisions}
        for revision in fallback_revisions:
            if on_revision is not None and revision.change_id not in streamed_change_ids:
                on_revision(revision, False)
        return StatusResult(
            github_error=github_error,
            github_repository=github_repository.full_name,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            revisions=fallback_revisions,
            selected_revset=selected_revset,
            base_parent_subject=base_parent_subject,
            submitted_state_disagreements=submitted_disagreements,
        )

    if not github_status_reported:
        emit_github_status(None)
    revisions_by_change_id = {revision.change_id: revision for revision in revisions}
    display_revisions = tuple(
        revisions_by_change_id.get(revision.change_id, revision)
        for revision in fallback_revisions
    )
    if persist_cache_updates:
        _persist_status_cache_updates(prepared=prepared, revisions=display_revisions)
    return StatusResult(
        github_error=None,
        github_repository=github_repository.full_name,
        incomplete=_status_is_incomplete(display_revisions),
        remote=prepared.remote,
        remote_error=None,
        revisions=display_revisions,
        selected_revset=selected_revset,
        base_parent_subject=base_parent_subject,
        submitted_state_disagreements=submitted_disagreements,
    )


def prepare_stack_for_status(
    *,
    config: RepoConfig,
    jj_client: JjClient,
    persist_bookmarks: bool,
    remote: GitRemote | None,
    remote_error: ErrorMessage | None,
    stack: LocalStack,
    state: ReviewState,
    state_store: ReviewStateStore,
    bookmark_states: dict[str, BookmarkState] | None = None,
) -> PreparedStack:
    """Build prepared status inputs for one already-resolved local stack."""

    pinned_bookmarks = pinned_bookmarks_for_revisions(revisions=stack.revisions, state=state)
    if bookmark_states is None:
        bookmark_states = {}
        if remote is not None or config.use_bookmarks:
            bookmark_states = jj_client.list_bookmark_states(pinned_bookmarks)

    matched_bookmarks = match_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        patterns=tuple(config.use_bookmarks),
        revisions=stack.revisions,
        remote_name=remote.name if remote is not None else None,
    )
    discovered_bookmarks: dict[str, str] = {}
    if remote is not None and pinned_bookmarks is None:
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
    if persist_bookmarks and bookmark_result.changed:
        state_store.save(bookmark_result.state)

    state_changes = dict(bookmark_result.state.changes if persist_bookmarks else state.changes)
    status_revisions = tuple(
        PreparedRevision(
            bookmark=resolution.bookmark,
            bookmark_source=resolution.source,
            cached_change=(
                state_changes.get(revision.change_id) or state.changes.get(revision.change_id)
            ),
            revision=revision,
        )
        for resolution, revision in zip(
            bookmark_result.resolutions,
            stack.revisions,
            strict=True,
        )
    )
    return PreparedStack(
        bookmark_states=bookmark_states,
        bookmark_result_changed=persist_bookmarks and bookmark_result.changed,
        client=jj_client,
        remote=remote,
        remote_error=remote_error,
        stack=stack,
        state=state,
        state_changes=state_changes,
        state_store=state_store,
        status_revisions=status_revisions,
    )


def pinned_bookmarks_for_revisions(
    *,
    revisions: tuple[LocalRevision, ...],
    state: ReviewState,
) -> tuple[str, ...] | None:
    """Return pinned bookmark names if every revision is already pinned, else None.

    Used to avoid listing every repo bookmark when rediscovery is impossible: if
    every revision already has a saved bookmark, bookmark matching has nothing
    to look for.
    """

    pinned: list[str] = []
    for revision in revisions:
        cached = state.changes.get(revision.change_id)
        if cached is not None and cached.bookmark:
            pinned.append(cached.bookmark)
            continue
        return None
    return tuple(dict.fromkeys(pinned))


def build_status_revisions_for_prepared_stack(
    prepared: PreparedStack,
    *,
    pull_request_lookups: dict[str, PullRequestLookup] | None = None,
) -> tuple[ReviewStatusRevision, ...]:
    return tuple(
        ReviewStatusRevision(
            bookmark=revision.bookmark,
            bookmark_source=revision.bookmark_source,
            cached_change=revision.cached_change,
            change_id=revision.revision.change_id,
            commit_id=revision.revision.commit_id,
            link_state=(
                revision.cached_change.link_state
                if revision.cached_change is not None
                else "active"
            ),
            local_divergent=revision.revision.divergent,
            pull_request_lookup=(
                pull_request_lookups.get(revision.bookmark)
                if pull_request_lookups is not None
                else None
            ),
            remote_state=(
                prepared.bookmark_states.get(
                    revision.bookmark,
                    BookmarkState(name=revision.bookmark),
                ).remote_target(prepared.remote.name)
                if prepared.remote is not None
                else None
            ),
            managed_comments_lookup=None,
            subject=revision.revision.subject,
        )
        for revision in prepared.status_revisions
    )


def _needs_github_inspection(
    prepared_revision: PreparedRevision,
    *,
    discover_remote_review: bool,
) -> bool:
    if discover_remote_review:
        return True
    return classify_saved_review_change(
        prepared_revision.cached_change,
        local="present",
    ).saved_review_identity


def _status_is_incomplete(revisions: tuple[ReviewStatusRevision, ...]) -> bool:
    for revision in revisions:
        change_status = classify_review_status_revision(revision)
        if change_status.local == "divergent" and change_status.pr_lifecycle != "merged":
            return True
        if (
            change_status.pr_lifecycle == "ambiguous"
            or change_status.has_pull_request_lookup_failure
            or change_status.has_stale_pull_request_link
        ):
            return True
        managed_comments_lookup = revision.managed_comments_lookup
        if managed_comments_lookup is not None and managed_comments_lookup.state in {
            "ambiguous",
            "error",
        }:
            return True
    return False


def _resolved_review_decision(
    *,
    cached_change: CachedChange | None,
    pull_request_lookup: PullRequestLookup,
) -> str | None:
    if pull_request_lookup.review_decision_error is None:
        return pull_request_lookup.review_decision
    if cached_change is None:
        return None
    return cached_change.pr_review_decision


def _persist_status_cache_updates(
    *,
    prepared: PreparedStack,
    revisions: tuple[ReviewStatusRevision, ...],
) -> None:
    state_changes = dict(prepared.state_changes)
    for revision in revisions:
        cached_change = state_changes.get(revision.change_id) or prepared.state.changes.get(
            revision.change_id
        )
        updated_change = cached_change
        if cached_change is not None and cached_change.is_unlinked:
            if updated_change != cached_change:
                state_changes[revision.change_id] = cached_change
            continue
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is not None:
            if updated_change is None and pull_request_lookup.state != "missing":
                updated_change = CachedChange(
                    bookmark=revision.bookmark,
                    bookmark_ownership=bookmark_ownership_for_source(
                        revision.bookmark_source
                    ),
                )
            if pull_request_lookup.state == "missing":
                if updated_change is not None:
                    updated_change = updated_change.model_copy(
                        update={
                            "bookmark": revision.bookmark,
                            "bookmark_ownership": bookmark_ownership_for_source(
                                revision.bookmark_source
                            ),
                        }
                    )
            elif pull_request_lookup.pull_request is not None:
                if updated_change is None:
                    raise AssertionError("Pull request lookup must create cached state.")
                pull_request = pull_request_lookup.pull_request
                updated_change = updated_change.model_copy(
                    update={
                        "bookmark": revision.bookmark,
                        "bookmark_ownership": bookmark_ownership_for_source(
                            revision.bookmark_source
                        ),
                        "pr_is_draft": pull_request.is_draft,
                        "pr_number": pull_request.number,
                        "pr_review_decision": _resolved_review_decision(
                            cached_change=cached_change,
                            pull_request_lookup=pull_request_lookup,
                        ),
                        "pr_state": pull_request.state,
                        "pr_url": pull_request.html_url,
                    }
                )
                if pull_request_lookup.state != "open":
                    updated_change = updated_change.model_copy(
                        update={
                            "navigation_comment_id": None,
                            "overview_comment_id": None,
                        }
                    )
        managed_comments_lookup = revision.managed_comments_lookup
        if managed_comments_lookup is not None:
            if updated_change is None:
                updated_change = CachedChange(
                    bookmark=revision.bookmark,
                    bookmark_ownership=bookmark_ownership_for_source(
                        revision.bookmark_source
                    ),
                )
            if managed_comments_lookup.state == "resolved":
                updated_change = updated_change.model_copy(
                    update={
                        "navigation_comment_id": (
                            None
                            if managed_comments_lookup.navigation_comment is None
                            else managed_comments_lookup.navigation_comment.id
                        ),
                        "overview_comment_id": (
                            None
                            if managed_comments_lookup.overview_comment is None
                            else managed_comments_lookup.overview_comment.id
                        ),
                    }
                )
        if updated_change is not None and updated_change != cached_change:
            state_changes[revision.change_id] = updated_change

    next_state = prepared.state.model_copy(update={"changes": state_changes})
    if next_state != prepared.state:
        prepared.state_store.save(next_state)


async def _iter_status_revisions_with_github(
    *,
    github_repository: ParsedGithubRepo,
    inspect_stack_comments: bool,
    on_github_status: Callable[[str | None], None] | None,
    prepared: PreparedStack,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> AsyncIterator[ReviewStatusRevision]:
    ordered_prepared_revisions = tuple(reversed(prepared_revisions))
    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        pull_request_lookups = await _resolve_pull_request_lookups(
            github_client=github_client,
            github_repository=github_repository,
            on_progress=None,
            prepared_revisions=ordered_prepared_revisions,
        )
        if on_github_status is not None:
            on_github_status(None)
        semaphore = asyncio.Semaphore(_GITHUB_INSPECTION_CONCURRENCY)
        tasks = tuple(
            asyncio.create_task(
                _inspect_revision_with_github(
                    bookmark_states=prepared.bookmark_states,
                    github_client=github_client,
                    github_repository=github_repository,
                    inspect_stack_comments=inspect_stack_comments,
                    prepared=prepared,
                    prepared_revision=prepared_revision,
                    pull_request_lookup=pull_request_lookups[prepared_revision.bookmark],
                    semaphore=semaphore,
                )
            )
            for prepared_revision in ordered_prepared_revisions
        )
        try:
            for task in tasks:
                yield await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def lookup_pull_request_lookups(
    *,
    github_repository: ParsedGithubRepo,
    on_progress: Callable[[int], None] | None = None,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    """Return batched pull-request lookups keyed by bookmark."""

    return asyncio.run(
        lookup_pull_request_lookups_async(
            github_repository=github_repository,
            on_progress=on_progress,
            prepared_revisions=prepared_revisions,
        )
    )


async def lookup_pull_request_lookups_async(
    *,
    github_repository: ParsedGithubRepo,
    on_progress: Callable[[int], None] | None = None,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    """Return batched pull-request lookups keyed by bookmark."""

    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        return await _resolve_pull_request_lookups(
            github_client=github_client,
            github_repository=github_repository,
            on_progress=on_progress,
            prepared_revisions=prepared_revisions,
        )


async def _inspect_revision_with_github(
    *,
    bookmark_states: dict[str, BookmarkState],
    github_client: GithubClient,
    github_repository,
    inspect_stack_comments: bool,
    prepared: PreparedStack,
    prepared_revision: PreparedRevision,
    pull_request_lookup: PullRequestLookup,
    semaphore: asyncio.Semaphore,
) -> ReviewStatusRevision:
    async with semaphore:
        bookmark_state = bookmark_states.get(
            prepared_revision.bookmark,
            BookmarkState(name=prepared_revision.bookmark),
        )
        remote_state = (
            bookmark_state.remote_target(prepared.remote.name) if prepared.remote else None
        )
        managed_comments_lookup: ManagedCommentsLookup | None = None
        if inspect_stack_comments and pull_request_lookup.state == "open":
            pull_request = pull_request_lookup.pull_request
            if pull_request is None:
                raise AssertionError("Open pull request lookup must include a pull request.")
            managed_comments_lookup = await _inspect_managed_comments(
                github_client=github_client,
                github_repository=github_repository,
                pull_request_number=pull_request.number,
            )
        logger.debug(
            "status revision inspected: change_id=%s bookmark=%s pr_state=%s",
            short_change_id(prepared_revision.revision.change_id),
            prepared_revision.bookmark,
            pull_request_lookup.state,
        )
        return ReviewStatusRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            cached_change=prepared_revision.cached_change,
            change_id=prepared_revision.revision.change_id,
            commit_id=prepared_revision.revision.commit_id,
            link_state=(
                prepared_revision.cached_change.link_state
                if prepared_revision.cached_change is not None
                else "active"
            ),
            local_divergent=prepared_revision.revision.divergent,
            pull_request_lookup=pull_request_lookup,
            remote_state=remote_state,
            managed_comments_lookup=managed_comments_lookup,
            subject=prepared_revision.revision.subject,
        )


async def _resolve_pull_request_lookups(
    *,
    github_client: GithubClient,
    github_repository,
    on_progress: Callable[[int], None] | None,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    pull_request_lookups = await _discover_pull_request_lookups(
        github_client=github_client,
        github_repository=github_repository,
        prepared_revisions=prepared_revisions,
    )
    if on_progress is not None and pull_request_lookups:
        on_progress(len(pull_request_lookups))
    return pull_request_lookups


async def _discover_pull_request_lookups(
    *,
    github_client: GithubClient,
    github_repository,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    prepared_revisions_by_bookmark = {
        prepared_revision.bookmark: prepared_revision
        for prepared_revision in prepared_revisions
    }
    bookmarks = tuple(prepared_revisions_by_bookmark)
    if not bookmarks:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=bookmarks,
        )
    except GithubClientError as error:
        if _is_repository_level_github_lookup_error(error):
            raise CliError("") from error
        lookup_error = summarize_github_lookup_error(
            action="pull request lookup",
            error=error,
        )
        return {
            bookmark: PullRequestLookup(
                message=lookup_error,
                pull_request=None,
                repository_error=None,
                state="error",
            )
            for bookmark in bookmarks
        }

    lookups = {
        bookmark: _pull_request_lookup_from_discovered(
            head_label=t"{github_repository.owner}:{ui.bookmark(bookmark)}",
            pull_requests=discovered_pull_requests.get(bookmark, ()),
        )
        for bookmark in bookmarks
    }
    remembered_numbers = tuple(
        prepared_revision.cached_change.pr_number
        for bookmark, prepared_revision in prepared_revisions_by_bookmark.items()
        if lookups[bookmark].state == "missing"
        and prepared_revision.cached_change is not None
        and prepared_revision.cached_change.pr_number is not None
    )
    if not remembered_numbers:
        return lookups

    try:
        remembered_pull_requests = await github_client.get_pull_requests_by_numbers(
            github_repository.owner,
            github_repository.repo,
            pull_numbers=remembered_numbers,
        )
    except GithubClientError as error:
        lookup_error = summarize_github_lookup_error(
            action="remembered pull request lookup",
            error=error,
        )
        failed_lookups: dict[str, PullRequestLookup] = {}
        for bookmark, lookup in lookups.items():
            cached_change = prepared_revisions_by_bookmark[bookmark].cached_change
            if (
                lookup.state == "missing"
                and cached_change is not None
                and cached_change.pr_number is not None
            ):
                failed_lookups[bookmark] = PullRequestLookup(
                    message=lookup_error,
                    pull_request=None,
                    repository_error=None,
                    state="error",
                )
            else:
                failed_lookups[bookmark] = lookup
        return failed_lookups

    for bookmark, lookup in tuple(lookups.items()):
        if lookup.state != "missing":
            continue
        cached_change = prepared_revisions_by_bookmark[bookmark].cached_change
        if cached_change is None or cached_change.pr_number is None:
            continue
        remembered_pull_request = remembered_pull_requests.get(cached_change.pr_number)
        if remembered_pull_request is None:
            continue
        lookups[bookmark] = _pull_request_lookup_from_remembered(
            bookmark=bookmark,
            pull_request=remembered_pull_request,
        )
    return lookups


def _pull_request_lookup_from_discovered(
    *,
    head_label: Message,
    pull_requests: tuple[GithubPullRequest, ...],
) -> PullRequestLookup:
    if not pull_requests:
        return PullRequestLookup(
            message=None,
            pull_request=None,
            repository_error=None,
            state="missing",
        )
    if len(pull_requests) > 1:
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        return PullRequestLookup(
            message=(
                t"GitHub reports multiple pull requests for head branch "
                t"{head_label}: {numbers}."
            ),
            pull_request=None,
            repository_error=None,
            state="ambiguous",
        )

    pull_request = pull_requests[0]
    effective_pull_request = pull_request.normalize_state()
    if effective_pull_request.state != "open":
        return PullRequestLookup(
            message=(
                t"GitHub reports pull request #{effective_pull_request.number} "
                t"for head branch {head_label} in state "
                t"{effective_pull_request.state}."
            ),
            pull_request=effective_pull_request,
            review_decision=None,
            repository_error=None,
            state="closed",
        )
    return PullRequestLookup(
        message=None,
        pull_request=effective_pull_request,
        review_decision=(
            None
            if effective_pull_request.is_draft
            else effective_pull_request.review_decision
        ),
        review_decision_error=None,
        repository_error=None,
        state="open",
    )


def _pull_request_lookup_from_remembered(
    *,
    bookmark: str,
    pull_request: GithubPullRequest,
) -> PullRequestLookup:
    effective_pull_request = pull_request.normalize_state()
    message: ErrorMessage | None = None
    if effective_pull_request.head.ref != bookmark:
        message = (
            t"Remembered PR #{effective_pull_request.number} now uses head branch "
            t"{ui.bookmark(effective_pull_request.head.ref)}, not "
            t"{ui.bookmark(bookmark)}."
        )
    if effective_pull_request.state != "open":
        return PullRequestLookup(
            message=message,
            pull_request=effective_pull_request,
            review_decision=None,
            repository_error=None,
            source="remembered",
            state="closed",
        )
    return PullRequestLookup(
        message=message,
        pull_request=effective_pull_request,
        review_decision=(
            None
            if effective_pull_request.is_draft
            else effective_pull_request.review_decision
        ),
        review_decision_error=None,
        repository_error=None,
        source="remembered",
        state="open",
    )


async def _inspect_managed_comments(
    *,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> ManagedCommentsLookup:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        return ManagedCommentsLookup(
            message=summarize_github_lookup_error(
                action=f"stack comment lookup for pull request #{pull_request_number}",
                error=error,
            ),
            navigation_comment=None,
            overview_comment=None,
            state="error",
        )

    navigation_comments = [comment for comment in comments if is_navigation_comment(comment.body)]
    overview_comments = [comment for comment in comments if is_overview_comment(comment.body)]
    messages: list[str] = []
    if len(navigation_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in navigation_comments)
        messages.append(
            "GitHub reports multiple jj-review stack navigation comments for the same "
            f"request: {comment_ids}."
        )
    if len(overview_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in overview_comments)
        messages.append(
            "GitHub reports multiple jj-review stack overview comments for the same "
            f"request: {comment_ids}."
        )
    if messages:
        return ManagedCommentsLookup(
            message=" ".join(messages),
            navigation_comment=None,
            overview_comment=None,
            state="ambiguous",
        )
    return ManagedCommentsLookup(
        message=None,
        navigation_comment=navigation_comments[0] if navigation_comments else None,
        overview_comment=overview_comments[0] if overview_comments else None,
        state="resolved",
    )


def _is_repository_level_github_lookup_error(error: GithubClientError) -> bool:
    if error.status_code is None:
        return True
    if error.status_code in {401, 403, 404}:
        return True
    return error.status_code >= 500


def _change_id_resolves(client: JjClient, change_id: str) -> bool:
    """Return True if the change_id resolves to a visible revision in the local repo."""
    try:
        client.resolve_revision(change_id)
        return True
    except CliError:
        return False
