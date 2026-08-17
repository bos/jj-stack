"""Change and pull-request selection helpers for command modules."""

from __future__ import annotations

from collections.abc import Sequence

import jj_stack.ui as ui
from jj_stack.errors import AmbiguousSelectionError, CliError, UsageError
from jj_stack.github.pr_refs import (
    parse_pr_number,
    parse_repo_pr_reference,
)
from jj_stack.github.resolution import parse_github_repo, select_submit_remote
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore


def resolve_selected_revset(
    *,
    command_label: str,
    default_revset: str | None = None,
    require_explicit: bool,
    revset: str | None,
) -> str | None:
    """Resolve an optional `<revset>` for change-oriented commands."""

    if revset is not None:
        return revset
    if require_explicit:
        raise UsageError(t"{ui.cmd(command_label)} requires an explicit change selection.")
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


def resolve_linked_change_for_pr(
    *,
    jj_client: JjClient,
    pr_reference: str,
    revset: str | None,
) -> tuple[int, str]:
    """Resolve `--pull-request` to one linked visible local change ID."""

    if revset is not None:
        raise UsageError(
            t"Use either {ui.cmd('<revset>')} or {ui.cmd('--pull-request')}, not both."
        )

    pr_number = resolve_pr_number(
        jj_client=jj_client,
        pr_reference=pr_reference,
    )
    state = TrackingStore.for_repo(jj_client.repo_root).load()
    matching_change_ids = [
        change_id
        for change_id, pr_identity in state.pr_identities.items()
        if pr_identity.pr_number == pr_number
    ]
    if not matching_change_ids:
        raise CliError(
            t"PR #{pr_number} is not linked to any local change.",
            hint=(
                t"Use an explicit change instead, or run {ui.cmd('checkout')} or "
                t"{ui.cmd('relink')} first."
            ),
        )
    if len(matching_change_ids) > 1:
        raise AmbiguousSelectionError(
            t"PR #{pr_number} is linked to multiple local changes.",
            hint=t"Use an explicit change ID after pointing the remote at the intended repo.",
        )

    return pr_number, matching_change_ids[0]


def resolve_pr_number(
    *,
    jj_client: JjClient,
    pr_reference: str,
) -> int:
    """Resolve a pull-request selector as a pull request number for this repo."""

    pr_number = parse_pr_number(pr_reference)
    if pr_number is not None:
        return pr_number

    remotes = jj_client.list_git_remotes()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        raise CliError(
            t"Could not determine the GitHub repo for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote.",
            hint=error.hint,
        ) from error
    github_repo = parse_github_repo(remote)
    if github_repo is None:
        raise CliError(
            t"Could not determine the GitHub repo for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote."
        )

    return parse_repo_pr_reference(
        reference=pr_reference,
        github_repo=github_repo,
    )
