"""Resolve bookmark mutations and the push strategy for each stack revision."""

from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.bookmarks import (
    BookmarkResolutionResult,
    BookmarkSource,
    ResolvedBookmark,
)
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_change_without_pull_request,
)

from .models import (
    LocalBookmarkAction,
    PreparedSubmitRevision,
    PushOperation,
    RemoteBookmarkAction,
    RemoteBookmarkSyncer,
    SubmitMutationRun,
)


@dataclass(frozen=True, slots=True)
class _ClassifiedRevision:
    """One stack revision with its resolved bookmark and review classification."""

    bookmark: str
    bookmark_source: BookmarkSource
    bookmark_state: BookmarkState
    cached_change: CachedChange | None
    remote_state: RemoteBookmarkState | None
    review_status: ReviewChangeStatus
    revision: LocalRevision


def _classify_revision(
    *,
    bookmark_states: dict[str, BookmarkState],
    remote: GitRemote,
    resolution: ResolvedBookmark,
    revision: LocalRevision,
    state: ReviewState,
) -> _ClassifiedRevision:
    bookmark_state = bookmark_states.get(
        resolution.bookmark,
        BookmarkState(name=resolution.bookmark),
    )
    cached_change = state.changes.get(revision.change_id)
    remote_state = bookmark_state.remote_target(remote.name)
    return _ClassifiedRevision(
        bookmark=resolution.bookmark,
        bookmark_source=resolution.source,
        bookmark_state=bookmark_state,
        cached_change=cached_change,
        remote_state=remote_state,
        review_status=classify_review_change_without_pull_request(
            cached_change=cached_change,
            commit_id=revision.commit_id,
            remote_state=remote_state,
        ),
        revision=revision,
    )


def prepare_submit_revisions(
    *,
    bookmark_result: BookmarkResolutionResult,
    bookmark_states: dict[str, BookmarkState],
    client: JjClient,
    remote: GitRemote,
    stack: LocalStack,
) -> tuple[PreparedSubmitRevision, ...]:
    """Resolve bookmark mutations and push strategy for each stack revision."""

    classified = tuple(
        _classify_revision(
            bookmark_states=bookmark_states,
            remote=remote,
            resolution=resolution,
            revision=revision,
            state=bookmark_result.state,
        )
        for resolution, revision in zip(
            bookmark_result.resolutions,
            stack.revisions,
            strict=True,
        )
    )
    actual_remote_targets = _load_actual_remote_targets_for_saved_bookmarks(
        classified=classified,
        client=client,
        remote=remote,
    )
    for entry in classified:
        _ensure_actual_remote_target_is_safe(
            actual_remote_targets=actual_remote_targets,
            entry=entry,
            remote=remote.name,
        )

    prepared_revisions: list[PreparedSubmitRevision] = []
    for entry in classified:
        ensure_change_is_not_unlinked(
            change_id=entry.revision.change_id,
            review_status=entry.review_status,
        )
        local_action = _resolve_local_action(
            entry.bookmark,
            entry.bookmark_state.local_targets,
            entry.revision.commit_id,
        )
        _ensure_remote_can_be_updated(entry, remote=remote.name)

        push_operation, remote_action, expected_remote_target = _remote_push_plan(
            remote_state=entry.remote_state,
            review_status=entry.review_status,
        )

        prepared_revisions.append(
            PreparedSubmitRevision(
                bookmark=entry.bookmark,
                bookmark_source=entry.bookmark_source,
                expected_remote_target=expected_remote_target,
                local_action=local_action,
                push_operation=push_operation,
                remote_action=remote_action,
                revision=entry.revision,
            )
        )

    prepared = tuple(prepared_revisions)
    _preflight_atomic_remote_push_plan(prepared_revisions=prepared, remote=remote)
    return prepared


def sync_local_bookmarks(
    *,
    bookmark_result: BookmarkResolutionResult,
    bookmark_states: dict[str, BookmarkState],
    client: JjClient,
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    run: SubmitMutationRun,
) -> None:
    """Apply prepared local bookmark moves under the submit mutation journal."""

    bookmark_updates = tuple(
        prepared_revision
        for prepared_revision in prepared_revisions
        if prepared_revision.local_action != "unchanged"
    )
    if not bookmark_updates:
        return
    run.journal.append(
        "planned_mutation",
        {
            "bookmarks": tuple(
                {
                    "action": prepared_revision.local_action,
                    "bookmark": prepared_revision.bookmark,
                    "change_id": prepared_revision.revision.change_id,
                    "commit_id": prepared_revision.revision.commit_id,
                }
                for prepared_revision in bookmark_updates
            ),
            "mutation": "sync_local_bookmarks",
        },
    )
    if run.dry_run:
        return

    local_target_change_ids = _resolve_local_target_change_ids_for_bookmark_updates(
        bookmark_result=bookmark_result,
        bookmark_states=bookmark_states,
        client=client,
        bookmark_updates=bookmark_updates,
    )
    applied: list[dict[str, str]] = []
    for prepared_revision in bookmark_updates:
        bookmark_state = bookmark_states.get(
            prepared_revision.bookmark,
            BookmarkState(name=prepared_revision.bookmark),
        )
        allow_backwards = _bookmark_is_already_managed_for_change(
            bookmark=prepared_revision.bookmark,
            bookmark_state=bookmark_state,
            cached_change=bookmark_result.state.changes.get(prepared_revision.revision.change_id),
            change_id=prepared_revision.revision.change_id,
            local_target_change_ids=local_target_change_ids,
        )
        client.set_bookmark(
            prepared_revision.bookmark,
            prepared_revision.revision.commit_id,
            allow_backwards=allow_backwards,
        )
        applied.append(
            {
                "action": prepared_revision.local_action,
                "bookmark": prepared_revision.bookmark,
                "change_id": prepared_revision.revision.change_id,
                "commit_id": prepared_revision.revision.commit_id,
            }
        )

    run.journal.append(
        "mutation_applied",
        {
            "bookmarks": tuple(applied),
            "mutation": "sync_local_bookmarks",
        },
    )


def _resolve_local_target_change_ids_for_bookmark_updates(
    *,
    bookmark_result: BookmarkResolutionResult,
    bookmark_states: dict[str, BookmarkState],
    client: JjClient,
    bookmark_updates: tuple[PreparedSubmitRevision, ...],
) -> dict[str, str]:
    local_targets: list[str] = []
    for prepared_revision in bookmark_updates:
        cached_change = bookmark_result.state.changes.get(prepared_revision.revision.change_id)
        if _cached_change_manages_bookmark(
            bookmark=prepared_revision.bookmark,
            cached_change=cached_change,
        ):
            continue
        bookmark_state = bookmark_states.get(
            prepared_revision.bookmark,
            BookmarkState(name=prepared_revision.bookmark),
        )
        local_target = bookmark_state.local_target
        if local_target is not None:
            local_targets.append(local_target)

    if not local_targets:
        return {}
    revset = " | ".join(f"present('{target}')" for target in dict.fromkeys(local_targets))
    return {
        revision.commit_id: revision.change_id
        for revision in client.query_revisions(revset)
    }


def _remote_push_plan(
    *,
    remote_state: RemoteBookmarkState | None,
    review_status: ReviewChangeStatus,
) -> tuple[PushOperation, RemoteBookmarkAction, str | None]:
    if review_status.remote_branch_matches_commit is True:
        return "up_to_date", "up to date", None
    if review_status.remote_branch == "untracked":
        if remote_state is None or len(remote_state.targets) != 1:
            raise AssertionError("Checked remote target must be unambiguous.")
        target = remote_state.target
        if target is None:
            raise AssertionError("Checked remote target must exist.")
        return "git_update", "pushed", target
    return "batch", "pushed", None


def _preflight_atomic_remote_push_plan(
    *,
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote: GitRemote,
) -> None:
    """Reject push plans that cannot be applied as one atomic remote update."""

    remote_mutations = tuple(
        revision
        for revision in prepared_revisions
        if revision.push_operation in {"batch", "git_update"}
    )
    if len(remote_mutations) <= 1:
        return

    fallback_revisions = tuple(
        revision for revision in remote_mutations if revision.push_operation == "git_update"
    )
    if not fallback_revisions:
        return

    branches = ui.join(
        lambda revision: ui.bookmark(f"{revision.bookmark}@{remote.name}"),
        fallback_revisions,
    )
    raise CliError(
        t"Submit would need to update multiple review branches, but "
        t"{branches} are not tracked locally.",
        hint=(
            t"Fetch and track those review branches with "
            t"{ui.cmd('jj git fetch')} and {ui.cmd('jj bookmark track')}, "
            t"then retry so submit can push the stack as one atomic update."
        ),
    )


def _load_actual_remote_targets_for_saved_bookmarks(
    *,
    classified: tuple[_ClassifiedRevision, ...],
    client: JjClient,
    remote: GitRemote,
) -> dict[str, str]:
    bookmarks = tuple(
        sorted(
            {entry.bookmark for entry in classified if _saved_remote_target(entry) is not None}
        )
    )
    if not bookmarks:
        return {}
    return client.list_remote_branches(
        remote=remote.name,
        patterns=tuple(f"refs/heads/{bookmark}" for bookmark in bookmarks),
    )


def _saved_remote_target(entry: _ClassifiedRevision) -> str | None:
    """The submitted commit the saved record expects the remote branch to hold."""

    cached_change = entry.cached_change
    if (
        cached_change is None
        or entry.review_status.link != "active"
        or cached_change.bookmark != entry.bookmark
    ):
        return None
    return cached_change.last_submitted_commit_id


def _ensure_actual_remote_target_is_safe(
    *,
    actual_remote_targets: dict[str, str],
    entry: _ClassifiedRevision,
    remote: str,
) -> None:
    saved_target = _saved_remote_target(entry)
    if saved_target is None:
        return
    bookmark = entry.bookmark
    actual_target = actual_remote_targets.get(bookmark)
    if actual_target in {saved_target, entry.revision.commit_id}:
        return
    if actual_target is None:
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} no longer exists.",
            hint=(
                t"Fetch and inspect the PR link before submitting again. If this branch "
                t"should stay attached to this change, repair the link with relink."
            ),
        )
    raise CliError(
        t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} points to an unexpected commit.",
        hint=(
            t"Fetch and inspect the PR link before submitting again. If this branch "
            t"should stay attached to this change, repair the link with relink."
        ),
    )


def _bookmark_is_already_managed_for_change(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    cached_change: CachedChange | None,
    change_id: str,
    local_target_change_ids: dict[str, str],
) -> bool:
    """Whether `submit` is reasserting an already-managed bookmark for the same change.

    Same-change rewrites such as `jj split` can leave the bookmark pointing at a sibling
    of the desired commit (the other half of the split, or any post-rewrite commit that
    is not a descendant of the previous target). `jj bookmark set` refuses such
    "backwards or sideways" moves by default. The move is legitimate when the tool's
    tracking state already records this bookmark as managed for this change, or when
    the bookmark's current local target itself resolves to the same logical change as
    the desired commit. In either case `allow_backwards` is correct. For any other
    case the default guard stays in effect so an unrelated bookmark cannot be silently
    retargeted.

    A hidden `local_target` (e.g., abandoned by the user manually) is absent from the
    preloaded visible revision map. That keeps the default guard in effect, which is
    the safer behavior: forcing the move would require recovering a hidden commit's
    identity that we cannot prove.
    """

    if _cached_change_manages_bookmark(bookmark=bookmark, cached_change=cached_change):
        return True
    local_target = bookmark_state.local_target
    if local_target is None:
        return False
    return local_target_change_ids.get(local_target) == change_id


def _cached_change_manages_bookmark(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
) -> bool:
    return (
        cached_change is not None
        and cached_change.manages_bookmark
        and cached_change.bookmark == bookmark
    )


def _resolve_local_action(
    bookmark: str,
    local_targets: tuple[str, ...],
    desired_target: str,
) -> LocalBookmarkAction:
    if len(local_targets) > 1:
        raise CliError(
            t"Bookmark {ui.bookmark(bookmark)} has {len(local_targets)} conflicting "
            t"local targets.",
            hint=t"Resolve the bookmark conflict with {ui.cmd('jj bookmark')} before submitting.",
        )
    local_target = local_targets[0] if local_targets else None
    if local_target == desired_target:
        return "unchanged"
    if local_target is None:
        return "created"
    return "moved"


def _ensure_remote_can_be_updated(entry: _ClassifiedRevision, *, remote: str) -> None:
    review_status = entry.review_status
    if review_status.remote_branch == "absent":
        return
    if review_status.remote_branch == "conflicted":
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{entry.bookmark}@{remote}')} is conflicted. "
            t"Resolve it with {ui.cmd('jj git fetch')} and retry."
        )
    if review_status.remote_branch_matches_commit is True:
        return
    if _bookmark_link_is_proven(entry):
        return
    raise CliError(
        t"Remote bookmark {ui.bookmark(f'{entry.bookmark}@{remote}')} already exists and "
        t"points elsewhere. Submit will not take over an existing remote branch "
        t"unless its link is already proven by local state, tracking data, or "
        t"explicit relinking."
    )


def _bookmark_link_is_proven(entry: _ClassifiedRevision) -> bool:
    if entry.bookmark_state.local_target is not None:
        return True
    if entry.bookmark_source == "discovered":
        return True
    if entry.bookmark_source != "saved":
        return False
    return (
        entry.review_status.link == "active"
        and entry.cached_change is not None
        and entry.cached_change.bookmark == entry.bookmark
    )


def sync_remote_bookmarks(
    *,
    client: RemoteBookmarkSyncer,
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote: GitRemote,
    run: SubmitMutationRun,
) -> None:
    batch_push_bookmarks = tuple(
        prepared_revision.bookmark
        for prepared_revision in prepared_revisions
        if prepared_revision.push_operation == "batch"
    )
    if batch_push_bookmarks:
        run.journal.append(
            "planned_mutation",
            {
                "bookmarks": batch_push_bookmarks,
                "mutation": "push_review_bookmarks",
                "remote": remote.name,
            },
        )
        if not run.dry_run:
            client.push_bookmarks(
                remote=remote.name,
                bookmarks=batch_push_bookmarks,
            )
            run.journal.append(
                "mutation_applied",
                {
                    "bookmarks": batch_push_bookmarks,
                    "mutation": "push_review_bookmarks",
                    "remote": remote.name,
                },
            )

    for prepared_revision in prepared_revisions:
        if prepared_revision.push_operation != "git_update":
            continue
        run.journal.append(
            "planned_mutation",
            {
                "bookmark": prepared_revision.bookmark,
                "change_id": prepared_revision.revision.change_id,
                "commit_id": prepared_revision.revision.commit_id,
                "mutation": "update_untracked_remote_bookmark",
                "remote": remote.name,
            },
        )
        if not run.dry_run:
            if prepared_revision.expected_remote_target is None:
                raise AssertionError("Git remote update requires an expected target.")
            client.update_untracked_remote_bookmark(
                remote=remote.name,
                bookmark=prepared_revision.bookmark,
                desired_target=prepared_revision.revision.commit_id,
                expected_remote_target=prepared_revision.expected_remote_target,
            )
            run.journal.append(
                "mutation_applied",
                {
                    "bookmark": prepared_revision.bookmark,
                    "change_id": prepared_revision.revision.change_id,
                    "commit_id": prepared_revision.revision.commit_id,
                    "mutation": "update_untracked_remote_bookmark",
                    "remote": remote.name,
                },
            )


def ensure_change_is_not_unlinked(
    *,
    change_id: str,
    review_status: ReviewChangeStatus,
) -> None:
    if review_status.link != "unlinked":
        return
    raise CliError(
        t"Change {ui.change_id(change_id)} is unlinked from review tracking.",
        hint=t"Run {ui.cmd('relink')} to reattach it before submitting again.",
    )
