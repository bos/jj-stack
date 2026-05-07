"""Resolve bookmark mutations and the push strategy for each stack revision."""

from __future__ import annotations

from jj_review import ui
from jj_review.errors import CliError
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalStack
from jj_review.review.bookmarks import BookmarkResolutionResult, BookmarkSource

from .models import (
    LocalBookmarkAction,
    PreparedSubmitRevision,
    PushOperation,
    RemoteBookmarkAction,
    RemoteBookmarkSyncer,
)


def prepare_submit_revisions(
    *,
    bookmark_result: BookmarkResolutionResult,
    bookmark_states: dict[str, BookmarkState],
    client: JjClient,
    dry_run: bool,
    remote: GitRemote,
    stack: LocalStack,
) -> tuple[PreparedSubmitRevision, ...]:
    """Resolve bookmark mutations and push strategy for each stack revision."""

    prepared_revisions: list[PreparedSubmitRevision] = []
    actual_remote_targets = _load_actual_remote_targets_for_saved_bookmarks(
        bookmark_result=bookmark_result,
        client=client,
        remote=remote,
        stack=stack,
    )
    _preflight_actual_remote_targets(
        actual_remote_targets=actual_remote_targets,
        bookmark_result=bookmark_result,
        remote=remote,
        stack=stack,
    )
    local_bookmark_updates: list[tuple[PreparedSubmitRevision, BookmarkState]] = []
    for resolution, revision in zip(
        bookmark_result.resolutions,
        stack.revisions,
        strict=True,
    ):
        ensure_change_is_not_unlinked(
            cached_change=bookmark_result.state.changes.get(revision.change_id),
            change_id=revision.change_id,
        )
        bookmark_state = bookmark_states.get(
            resolution.bookmark,
            BookmarkState(name=resolution.bookmark),
        )
        local_action = _resolve_local_action(
            resolution.bookmark,
            bookmark_state.local_targets,
            revision.commit_id,
        )
        remote_state = bookmark_state.remote_target(remote.name)
        _ensure_remote_can_be_updated(
            bookmark=resolution.bookmark,
            bookmark_source=resolution.source,
            bookmark_state=bookmark_state,
            change_id=revision.change_id,
            desired_target=revision.commit_id,
            remote=remote.name,
            remote_state=remote_state,
            state=bookmark_result.state,
        )

        expected_remote_target: str | None = None
        if remote_state is not None and remote_state.target == revision.commit_id:
            push_operation: PushOperation = "up_to_date"
            remote_action: RemoteBookmarkAction = "up to date"
        elif (
            remote_state is not None
            and not remote_state.is_tracked
            and len(remote_state.targets) == 1
            and remote_state.target != revision.commit_id
        ):
            if remote_state is None:
                raise AssertionError("Checked remote bookmark state must exist.")
            expected_remote_target = remote_state.target
            if expected_remote_target is None:
                raise AssertionError("Checked remote target must be unambiguous.")
            push_operation = "git_update"
            remote_action = "pushed"
        else:
            push_operation = "batch"
            remote_action = "pushed"

        prepared_revision = PreparedSubmitRevision(
            bookmark=resolution.bookmark,
            bookmark_source=resolution.source,
            change_id=revision.change_id,
            expected_remote_target=expected_remote_target,
            local_action=local_action,
            push_operation=push_operation,
            remote_action=remote_action,
            revision=revision,
        )
        prepared_revisions.append(prepared_revision)
        local_bookmark_updates.append((prepared_revision, bookmark_state))

    prepared = tuple(prepared_revisions)
    _preflight_atomic_remote_push_plan(prepared_revisions=prepared, remote=remote)

    if not dry_run:
        for prepared_revision, bookmark_state in local_bookmark_updates:
            if prepared_revision.local_action == "unchanged":
                continue
            allow_backwards = _bookmark_is_already_managed_for_change(
                bookmark=prepared_revision.bookmark,
                bookmark_state=bookmark_state,
                cached_change=bookmark_result.state.changes.get(
                    prepared_revision.change_id
                ),
                change_id=prepared_revision.change_id,
                jj_client=client,
            )
            client.set_bookmark(
                prepared_revision.bookmark,
                prepared_revision.revision.commit_id,
                allow_backwards=allow_backwards,
            )

    return prepared


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
        revision
        for revision in remote_mutations
        if revision.push_operation == "git_update"
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
    bookmark_result: BookmarkResolutionResult,
    client: JjClient,
    remote: GitRemote,
    stack: LocalStack,
) -> dict[str, str]:
    bookmarks = tuple(
        sorted(
            {
                resolution.bookmark
                for resolution, revision in zip(
                    bookmark_result.resolutions,
                    stack.revisions,
                    strict=True,
                )
                if _cached_change_has_saved_remote_target(
                    bookmark_result.state.changes.get(revision.change_id),
                    resolution.bookmark,
                )
            }
        )
    )
    if not bookmarks:
        return {}
    return client.list_remote_branches(
        remote=remote.name,
        patterns=tuple(f"refs/heads/{bookmark}" for bookmark in bookmarks),
    )


def _preflight_actual_remote_targets(
    *,
    actual_remote_targets: dict[str, str],
    bookmark_result: BookmarkResolutionResult,
    remote: GitRemote,
    stack: LocalStack,
) -> None:
    for resolution, revision in zip(
        bookmark_result.resolutions,
        stack.revisions,
        strict=True,
    ):
        _ensure_actual_remote_target_is_safe(
            actual_remote_targets=actual_remote_targets,
            bookmark=resolution.bookmark,
            cached_change=bookmark_result.state.changes.get(revision.change_id),
            desired_target=revision.commit_id,
            remote=remote.name,
        )


def _cached_change_has_saved_remote_target(
    cached_change: CachedChange | None,
    bookmark: str,
) -> bool:
    return (
        cached_change is not None
        and not cached_change.is_unlinked
        and cached_change.bookmark == bookmark
        and cached_change.last_submitted_commit_id is not None
    )


def _ensure_actual_remote_target_is_safe(
    *,
    actual_remote_targets: dict[str, str],
    bookmark: str,
    cached_change: CachedChange | None,
    desired_target: str,
    remote: str,
) -> None:
    if not _cached_change_has_saved_remote_target(cached_change, bookmark):
        return
    if cached_change is None:
        raise AssertionError("Checked cached change must exist.")
    saved_target = cached_change.last_submitted_commit_id
    if saved_target is None:
        raise AssertionError("Checked cached change must have a saved submitted commit.")
    actual_target = actual_remote_targets.get(bookmark)
    if actual_target in {saved_target, desired_target}:
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
        t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} points to an "
        t"unexpected commit.",
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
    jj_client: JjClient,
) -> bool:
    """Whether `submit` is reasserting an already-managed bookmark for the same change.

    Same-change rewrites such as `jj split` can leave the bookmark pointing at a sibling
    of the desired commit (the other half of the split, or any post-rewrite commit that
    is not a descendant of the previous target). `jj bookmark set` refuses such
    "backwards or sideways" moves by default. The move is legitimate when the tool's
    saved state already records this bookmark as managed for this change, or when the
    bookmark's current local target itself resolves to the same logical change as the
    desired commit. In either case `allow_backwards` is correct. For any other case the
    default guard stays in effect so an unrelated bookmark cannot be silently
    retargeted.

    A hidden `local_target` (e.g., abandoned by the user manually) returns False on the
    same-change-id branch because `query_revisions` does not surface hidden revisions.
    That keeps the default guard in effect, which is the safer behavior: forcing the
    move would require recovering a hidden commit's identity that we cannot prove.
    """

    if (
        cached_change is not None
        and cached_change.manages_bookmark
        and cached_change.bookmark == bookmark
    ):
        return True
    local_target = bookmark_state.local_target
    if local_target is None:
        return False
    revisions = jj_client.query_revisions(f"'{local_target}'")
    return len(revisions) == 1 and revisions[0].change_id == change_id


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


def _ensure_remote_can_be_updated(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    desired_target: str,
    remote: str,
    remote_state: RemoteBookmarkState | None,
    state: ReviewState,
) -> None:
    if remote_state is None or not remote_state.targets:
        return
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} is conflicted. "
            t"Resolve it with {ui.cmd('jj git fetch')} and retry."
        )
    if remote_state.target == desired_target:
        return
    if _bookmark_link_is_proven(
        bookmark=bookmark,
        bookmark_source=bookmark_source,
        bookmark_state=bookmark_state,
        change_id=change_id,
        state=state,
    ):
        return
    raise CliError(
        t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} already exists and "
        t"points elsewhere. Submit will not take over an existing remote branch "
        t"unless its link is already proven by local state, tracking data, or "
        t"explicit relinking."
    )


def _bookmark_link_is_proven(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    state: ReviewState,
) -> bool:
    if bookmark_state.local_target is not None:
        return True
    if bookmark_source == "discovered":
        return True
    if bookmark_source != "saved":
        return False
    cached_change = state.changes.get(change_id)
    return (
        cached_change is not None
        and not cached_change.is_unlinked
        and cached_change.bookmark == bookmark
    )


def sync_remote_bookmarks(
    *,
    client: RemoteBookmarkSyncer,
    dry_run: bool,
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote: GitRemote,
) -> None:
    batch_push_bookmarks = tuple(
        prepared_revision.bookmark
        for prepared_revision in prepared_revisions
        if prepared_revision.push_operation == "batch"
    )
    if batch_push_bookmarks:
        if not dry_run:
            client.push_bookmarks(
                remote=remote.name,
                bookmarks=batch_push_bookmarks,
            )

    for prepared_revision in prepared_revisions:
        if prepared_revision.push_operation != "git_update":
            continue
        if not dry_run:
            if prepared_revision.expected_remote_target is None:
                raise AssertionError("Git remote update requires an expected target.")
            client.update_untracked_remote_bookmark(
                remote=remote.name,
                bookmark=prepared_revision.bookmark,
                desired_target=prepared_revision.revision.commit_id,
                expected_remote_target=prepared_revision.expected_remote_target,
            )


def ensure_change_is_not_unlinked(
    *,
    cached_change: CachedChange | None,
    change_id: str,
) -> None:
    if cached_change is None or not cached_change.is_unlinked:
        return
    raise CliError(
        t"Change {ui.change_id(change_id)} is unlinked from review tracking.",
        hint=t"Run {ui.cmd('relink')} to reattach it before submitting again.",
    )
