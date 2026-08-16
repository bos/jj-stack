"""Revision and pull-request selection helpers for command modules."""

from __future__ import annotations

from collections.abc import Sequence

import jj_stack.ui as ui
from jj_stack.errors import AmbiguousSelectionError, CliError, UsageError
from jj_stack.github.pull_request_refs import (
    parse_pull_request_number,
    parse_repository_pull_request_reference,
)
from jj_stack.github.resolution import parse_github_repo, select_submit_remote
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore


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
        raise UsageError(t"{ui.cmd(command_label)} requires an explicit revision selection.")
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


def resolve_linked_change_for_pull_request(
    *,
    jj_client: JjClient,
    pull_request_reference: str,
    revset: str | None,
) -> tuple[int, str]:
    """Resolve `--pull-request` to one linked visible local change ID."""

    if revset is not None:
        raise UsageError(
            t"Use either {ui.cmd('<revset>')} or {ui.cmd('--pull-request')}, not both."
        )

    pull_request_number = resolve_pull_request_number(
        jj_client=jj_client,
        pull_request_reference=pull_request_reference,
    )
    state = ReviewStateStore.for_repo(jj_client.repo_root).load()
    matching_change_ids = [
        change_id
        for change_id, review_identity in state.review_identities.items()
        if review_identity.pr_number == pull_request_number
    ]
    if not matching_change_ids:
        raise CliError(
            t"PR #{pull_request_number} is not linked to any local change.",
            hint=(
                t"Use an explicit revision instead, or run {ui.cmd('checkout')} or "
                t"{ui.cmd('relink')} first."
            ),
        )
    if len(matching_change_ids) > 1:
        raise AmbiguousSelectionError(
            t"PR #{pull_request_number} is linked to multiple local changes.",
            hint=t"Use an explicit change ID after pointing the remote at the intended "
            t"repository.",
        )

    return pull_request_number, matching_change_ids[0]


def resolve_pull_request_number(
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
