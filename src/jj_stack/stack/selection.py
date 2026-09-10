"""Change and pull-request selection helpers for command modules."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.errors import AmbiguousSelectionError, CliError, UsageError
from jj_stack.formatting import format_pr_label
from jj_stack.github.pr_refs import (
    parse_pr_number,
    parse_repo_pr_reference,
)
from jj_stack.github.resolution import GithubRepoAddress, parse_github_repo, select_submit_remote
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore


def resolve_linked_change_for_pr(
    *,
    jj_client: JjClient,
    pr_reference: str,
    revset: str | None,
) -> tuple[str, ui.Message]:
    """Resolve `--pull-request` to one linked local change ID, with the note that says so."""

    if revset is not None:
        raise UsageError(
            t"Use either {ui.cmd('<revset>')} or {ui.cmd('--pull-request')}, not both."
        )

    pr_number, repo = resolve_pr_reference(
        jj_client=jj_client,
        pr_reference=pr_reference,
    )
    pr_label = format_pr_label(pr_number, repo=repo)
    state = TrackingStore.for_repo(jj_client.repo_root).load()
    matching_change_ids = [
        change_id
        for change_id, tracked in state.prs.items()
        if tracked.pr_identity.pr_number == pr_number
    ]
    if not matching_change_ids:
        raise CliError(
            t"{pr_label} is not linked to any local change.",
            hint=(
                t"Fetch and link the PR stack with "
                t"{ui.cmd(f'jj-stack checkout --pull-request {pr_number}')}, or link an "
                t"existing local change with {ui.cmd(f'jj-stack relink {pr_number} CHANGE')}."
            ),
        )
    if len(matching_change_ids) > 1:
        raise AmbiguousSelectionError(
            t"{pr_label} is linked to multiple local changes.",
            hint=t"Run {ui.cmd('jj-stack list')} to find the conflicting saved links. Forget "
            t"the incorrect stack's links with {ui.cmd('jj-stack unstack --local CHANGE')}, "
            t"then link the intended change with {ui.cmd('jj-stack relink PR CHANGE')}.",
        )

    change_id = matching_change_ids[0]
    return change_id, t"Using {pr_label} for change {ui.change_id(change_id)}"


def resolve_pr_reference(
    *,
    jj_client: JjClient,
    pr_reference: str,
) -> tuple[int, GithubRepoAddress | None]:
    """Resolve a pull-request selector and its repo when one is available."""

    pr_number = parse_pr_number(pr_reference)
    remotes = jj_client.list_git_remotes()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        if pr_number is not None:
            return pr_number, None
        raise CliError(
            t"Could not determine the GitHub repo for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote.",
            hint=error.hint,
        ) from error
    github_repo = parse_github_repo(remote)
    if github_repo is None:
        if pr_number is not None:
            return pr_number, None
        raise CliError(
            t"Could not determine the GitHub repo for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote."
        )

    return (
        pr_number
        if pr_number is not None
        else parse_repo_pr_reference(reference=pr_reference, github_repo=github_repo),
        github_repo,
    )
