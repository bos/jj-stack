"""Revision and pull-request selection helpers for command modules."""

from __future__ import annotations

from collections.abc import Sequence

import jj_review.ui as ui
from jj_review.errors import CliError
from jj_review.github.pull_request_refs import (
    parse_pull_request_number,
    parse_repository_pull_request_reference,
)
from jj_review.github.resolution import parse_github_repo, select_submit_remote
from jj_review.jj.client import JjClient
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.change_status import classify_saved_review_change
from jj_review.review.discovery import discover_tracked_stacks
from jj_review.state.store import ReviewStateStore


def resolve_selected_revset(
    *,
    command_label: str,
    default_revset: str | None = None,
    require_explicit: bool,
    revset: str | None,
) -> str | None:
    """Resolve an optional `<revset>` for revision-oriented commands."""

    if revset is not None:
        return revset
    if require_explicit:
        raise CliError(
            t"{ui.cmd(command_label)} requires an explicit revision selection."
        )
    return default_revset


def parse_comma_separated_flag_values(
    values: Sequence[str] | None,
) -> list[str] | None:
    """Parse repeated comma-separated flag values into a deduplicated list."""

    if values is None:
        return None

    parsed_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in value.split(","):
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            parsed_values.append(normalized)
    return parsed_values


def resolve_orphaned_pull_request(
    *,
    jj_client: JjClient,
    pull_request_reference: str,
    state: ReviewState,
) -> tuple[int, str] | None:
    """Resolve `--pull-request` to a saved record whose change is not in any live stack.

    Returns `(pull_request_number, change_id)` only when:
    - exactly one tracked record matches the pull request number, and
    - that change is absent from every currently supported review stack.

    Returns `None` when the change still participates in a live stack (let the
    live-link path handle it) or when no matching tracked record exists (let
    the live-link path raise its targeted diagnostic).

    Raises `CliError` when two or more active tracked records claim the same
    pull request number. The tracking data is ambiguous; the user must repair
    it via `unlink` or `relink` before `unstack --cleanup --pull-request` can
    act, because there is no single orphan target to retire.

    The membership check matches what `list` renders as an `orphan` row: a
    visible-but-unsupported revision, for example one on a bookmark outside the
    review prefix or otherwise filtered out of stack discovery, should still be
    cleaned up via this path rather than routed back through the live-link
    selector.
    """

    pull_request_number = _parse_repo_pull_request_number(
        jj_client=jj_client,
        pull_request_reference=pull_request_reference,
    )
    matching_change_ids = _active_change_ids_for_pull_request(
        pull_request_number=pull_request_number,
        state=state,
    )
    if not matching_change_ids:
        return None
    if len(matching_change_ids) > 1:
        rendered = ", ".join(change_id[:8] for change_id in sorted(matching_change_ids))
        raise CliError(
            t"PR #{pull_request_number} is claimed by multiple tracked records ({rendered}).",
            hint=(
                t"Repair the tracking data with {ui.cmd('unlink')} "
                t"or {ui.cmd('relink')} before retrying."
            ),
        )
    change_id = matching_change_ids[0]
    discovered = discover_tracked_stacks(jj_client=jj_client, state=state)
    if any(
        revision.change_id == change_id
        for stack in discovered.stacks
        for revision in stack.revisions
    ):
        return None
    return pull_request_number, change_id


def resolve_pull_request_number(
    *,
    jj_client: JjClient,
    pull_request_reference: str,
) -> int:
    """Resolve a pull-request selector as a pull request number for this repo."""

    return _parse_repo_pull_request_number(
        jj_client=jj_client,
        pull_request_reference=pull_request_reference,
    )


def resolve_linked_change_for_pull_request(
    *,
    action_name: str,
    jj_client: JjClient,
    pull_request_reference: str,
    revset: str | None,
) -> tuple[int, str]:
    """Resolve `--pull-request` to one linked visible local change ID."""

    action_label = action_name.capitalize()
    if revset is not None:
        raise CliError(
            t"Use either {ui.cmd('<revset>')} or {ui.cmd('--pull-request')}, "
            t"not both."
        )

    pull_request_number = _parse_repo_pull_request_number(
        jj_client=jj_client,
        pull_request_reference=pull_request_reference,
    )
    state = ReviewStateStore.for_repo(jj_client.repo_root).load()
    matching_change_ids = _active_change_ids_for_pull_request(
        pull_request_number=pull_request_number,
        state=state,
    )
    if not matching_change_ids:
        raise CliError(
            t"PR #{pull_request_number} is not linked to any local change.",
            hint=(
                t"Use an explicit revision instead, or run {ui.cmd('import')} or "
                t"{ui.cmd('relink')} first."
            ),
        )
    if len(matching_change_ids) > 1:
        raise CliError(
            t"PR #{pull_request_number} is linked to multiple local changes.",
            hint=t"{action_label} by explicit revision after repairing the links.",
        )

    change_id = matching_change_ids[0]
    visible_revisions = jj_client.query_revisions_by_change_ids((change_id,)).get(
        change_id,
        (),
    )
    if not visible_revisions:
        raise CliError(
            t"PR #{pull_request_number} is linked to local change {ui.change_id(change_id)}, "
            t"but that change is not visible.",
            hint=t"{action_label} by revision once it is visible again.",
        )
    if len(visible_revisions) > 1:
        raise CliError(
            t"PR #{pull_request_number} is linked to local change {ui.change_id(change_id)}, "
            t"but that change is divergent.",
            hint=t"{action_label} by explicit revision after resolving it.",
        )
    return pull_request_number, change_id


def _active_change_ids_for_pull_request(
    *,
    pull_request_number: int,
    state: ReviewState,
) -> list[str]:
    return [
        change_id
        for change_id, cached_change in state.changes.items()
        if _saved_change_links_pull_request(
            cached_change,
            pull_request_number=pull_request_number,
        )
    ]


def _saved_change_links_pull_request(
    cached_change: CachedChange,
    *,
    pull_request_number: int,
) -> bool:
    review_status = classify_saved_review_change(cached_change, local="present")
    return (
        review_status.link == "active"
        and cached_change.pr_number == pull_request_number
    )


def _parse_repo_pull_request_number(
    *,
    jj_client: JjClient,
    pull_request_reference: str,
) -> int:
    """Resolve a pull-request selector as a pull request number for this repo."""

    pull_request_number = parse_pull_request_number(pull_request_reference)
    if pull_request_number is not None:
        return pull_request_number

    remotes = jj_client.list_git_remotes()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        raise CliError(
            t"Could not determine the GitHub repository for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote.",
            hint=error.hint,
        ) from error
    github_repository = parse_github_repo(remote)
    if github_repository is None:
        raise CliError(
            t"Could not determine the GitHub repository for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote."
        )

    return parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=github_repository,
    )
