"""Shared PR checks and cleanup helpers."""

from __future__ import annotations

from collections.abc import Callable

import jj_stack.ui as ui
from jj_stack.commands.cleanup.shared import CleanupAction
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_LABEL,
    delete_stack_overview_comment,
)
from jj_stack.identifiers import CommitId
from jj_stack.jj.client import JjClient, PRRefUpdate
from jj_stack.models.github import GithubIssueComment, GithubPR, GithubStack
from jj_stack.stack.change_state import (
    CompetingOpenPR,
    PRAmbiguous,
    PRIdentityMismatch,
    PRMissing,
    Unobserved,
    WithPR,
    classify,
)
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.ui import Message


def check_tracked_pr(*, change_id: str, observation: RepoFacts) -> WithPR | CleanupAction:
    """Return the classified saved PR, or the reason its identity cannot be trusted."""

    state = classify(observation.prs[change_id])
    if isinstance(state, (PRMissing, PRAmbiguous, PRIdentityMismatch)):
        return CleanupAction(
            kind="pull request",
            body=t"{state.reason}; {state.repair}",
            status="blocked",
        )
    return state


async def close_pr_on_trunk(
    *,
    github_client: GithubClient,
    pr: GithubPR,
    trunk_branch: str,
) -> Message | None:
    """Retarget one open PR to trunk, then close it, so GitHub can still reopen it later.

    GitHub refuses to retarget a closed PR and to reopen one whose base branch is gone.
    Returns why the PR is still open, or None once GitHub reports it closed.
    """

    pr_label = format_pr_label(pr.number, url=pr.html_url)
    try:
        if pr.base.ref != trunk_branch:
            pr = await github_client.update_pr(pr_number=pr.number, base=trunk_branch)
            if pr.state == "open" and pr.base.ref != trunk_branch:
                return (
                    t"cannot close {pr_label} because GitHub did not retarget it to "
                    t"{ui.bookmark(trunk_branch)}"
                )
        if pr.state == "open":
            await github_client.close_pr(pr_number=pr.number)
    except GithubClientError as error:
        return t"cannot close {pr_label}: {error.user_facing_reason()}"
    return None


async def apply_overview_comment_cleanup(
    *,
    comment: GithubIssueComment | None,
    dry_run: bool,
    github_client: GithubClient,
    pr_number: int,
) -> tuple[tuple[CleanupAction, ...], bool]:
    """Delete one overview comment identified during cleanup planning."""

    if comment is None:
        return (), True
    deleted = True
    if not dry_run:
        try:
            deleted = await delete_stack_overview_comment(
                comment_id=comment.id,
                github_client=github_client,
            )
        except CliError as error:
            return (
                CleanupAction(
                    kind=STACK_OVERVIEW_COMMENT_LABEL,
                    body=str(error),
                    status="blocked",
                ),
            ), False
    pr_label = format_pr_label(pr_number, repo=github_client.repo)
    action_body: Message = t"delete {STACK_OVERVIEW_COMMENT_LABEL} #{comment.id} from {pr_label}"
    if not dry_run and not deleted:
        action_body = (
            t"{STACK_OVERVIEW_COMMENT_LABEL} #{comment.id} already absent from {pr_label}"
        )
    return (
        CleanupAction(
            kind=STACK_OVERVIEW_COMMENT_LABEL,
            body=action_body,
            status="planned" if dry_run else "applied",
        ),
    ), True


def plan_pr_cleanup(
    *,
    observation: RepoFacts,
    preview_detached_dependents: frozenset[int] = frozenset(),
    state: WithPR,
) -> tuple[PRRefUpdate | None, CleanupAction | None]:
    """Plan branch deletion for a PR whose identity and lifecycle the caller checked."""

    pr = state.pr
    branch = pr.head.ref
    observed_dependents = observation.prs_by_base[branch]
    dependents = tuple(
        item
        for item in observed_dependents
        if item.number not in preview_detached_dependents
        # GitHub can never reopen a closed PR whose head branch is gone, so its base is free.
        and (item.state == "open" or item.head_branch_exists)
    )
    # A full 100-result page may hide another dependent, so it also fails closed.
    blockers = dependents[:1] or observed_dependents[99:100]
    if blockers:
        dependent = blockers[0]
        pr_label = format_pr_label(pr.number, url=pr.html_url)
        dependent_label = format_pr_label(dependent.number, url=dependent.html_url)
        recovery = (
            t"retarget {dependent_label} to its new base"
            if dependent.state == "open"
            else t"reopen and retarget {dependent_label} to its new base, or delete its head "
            t"branch if you no longer need to reopen it"
        )
        return None, CleanupAction(
            kind="remote branch",
            body=t"keep {pr_label}'s branch and saved link because "
            t"{dependent_label} still uses {ui.bookmark(branch)} "
            t"as its base; deleting the base branch would prevent reopening {dependent_label}. "
            t"To continue, {recovery}, then rerun {ui.cmd('jj-stack cleanup')}",
            status="blocked",
        )
    if isinstance(state, CompetingOpenPR):
        return (
            None,
            CleanupAction(
                kind="remote branch",
                body=t"cannot delete {ui.bookmark(branch)}: {state.reason}; {state.repair}",
                status="blocked",
            ),
        )
    configured_repo = observation.configured_repo
    remote_target = state.remote_target
    if (
        isinstance(remote_target, Unobserved)
        or configured_repo is None
        or configured_repo != observation.repo
    ):
        pr_label = format_pr_label(pr.number, repo=observation.repo)
        return (
            None,
            CleanupAction(
                kind="remote branch",
                body=t"cannot determine which Git remote belongs to {pr_label}; run "
                t"{ui.cmd('jj-stack doctor')} to check the repo setup",
                status="blocked",
            ),
        )
    update = (
        None
        if remote_target is None
        else PRRefUpdate(
            branch=branch,
            expected_target=CommitId(remote_target),
            desired_target=None,
        )
    )
    return update, None


def github_stack_cleanup_blockers(
    *,
    pr_numbers: tuple[int, ...],
    stacks: tuple[GithubStack, ...] | CliError,
) -> dict[int, CleanupAction]:
    """Block cleanup of selected PRs still needed by a GitHub stack with unmerged PRs.

    A merged member's branch is the base of the member above it, so a stack that still holds
    an active member needs every branch it groups, not only the active ones.
    """

    if isinstance(stacks, CliError):
        return dict.fromkeys(
            pr_numbers,
            CleanupAction(kind="remote branch", body=str(stacks), status="blocked"),
        )
    selected = set(pr_numbers)
    blockers: dict[int, CleanupAction] = {}
    for stack in stacks:
        if selected.isdisjoint(stack.active_pr_numbers):
            continue
        action = CleanupAction(
            kind="remote branch",
            body=(
                f"GitHub stack #{stack.number} still groups this pull request. "
                f"Run jj-stack unstack --stack {stack.number} and retry."
            ),
            status="blocked",
        )
        blockers.update({number: action for number in stack.pr_numbers if number in selected})
    return blockers


def apply_remote_branch_cleanup(
    *,
    dry_run: bool,
    jj_client: JjClient,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
    update: PRRefUpdate | None,
) -> None:
    """Delete a branch only if it still points to the commit checked during planning.

    A rejected lease raises, so there is no failure for callers to branch on.
    """

    if update is not None:
        if not dry_run:
            jj_client.mutate_remote_pr_branch_refs(
                remote=remote_name,
                updates=(update,),
            )
        record_action(
            CleanupAction(
                kind="remote branch",
                body=t"delete {ui.bookmark(f'{update.branch}@{remote_name}')}",
                status="planned" if dry_run else "applied",
            )
        )
