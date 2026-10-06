"""Mutation guards for GitHub stack resources."""

from __future__ import annotations

from collections.abc import Collection, Sequence

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack


def require_merged_prefix(stack: GithubStack) -> GithubStack:
    """Stop on a stack whose merged members do not all sit below its active ones.

    GitHub produces that shape when a parent PR branch is pushed to contain its child's head:
    the child is merged into the parent while the parent stays open. Planning indexes on the
    merged prefix, so such a stack can only be dissolved.
    """

    if not stack.has_merged_prefix:
        raise CliError(
            t"GitHub stack #{stack.number} lists a merged pull request above an unmerged one.",
            hint=t"Remove the GitHub stack with "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')}, then retry.",
        )
    return stack


def selected_github_stack(
    repo: GithubRepoAddress,
    selected_pr_numbers: Collection[int],
    stacks: Sequence[GithubStack],
) -> GithubStack | None:
    """Return the one GitHub stack the selected pull requests belong to.

    The selection may overlap at most one resource, and every active member of that resource
    must be selected. A merged prefix GitHub retains is always valid, so a resource the selection
    touches only through merged members is still returned for callers that reconcile them.
    Callers that mutate a resource separately require a selected pull request to remain active.
    """

    selected = set(selected_pr_numbers)
    overlapping = tuple(stack for stack in stacks if not selected.isdisjoint(stack.pr_numbers))
    if not overlapping:
        return None
    # Only a resource the selection still has an active member in can be dissolved. GitHub keeps
    # merged members forever, so unstacking a fully merged resource changes nothing, and naming
    # one in a hint would leave the user retrying a command that cannot make progress.
    dissolvable = tuple(
        stack for stack in overlapping if not selected.isdisjoint(stack.active_pr_numbers)
    )
    if len(dissolvable) > 1:
        numbers = tuple(sorted(stack.number for stack in dissolvable))
        raise CliError(
            t"The selected pull requests belong to GitHub stacks "
            t"{ui.join(lambda number: f'#{number}', numbers)}.",
            hint=t"Run "
            t"{ui.join(lambda number: ui.cmd(f'jj-stack unstack --stack {number}'), numbers)}, "
            t"then retry.",
        )
    if not dissolvable and len(overlapping) > 1:
        numbers = tuple(sorted(stack.number for stack in overlapping))
        raise CliError(
            t"The selected pull requests were merged in different GitHub stacks: "
            t"{ui.join(lambda number: f'#{number}', numbers)}.",
            hint="Select changes belonging to one of those stacks, then retry.",
        )
    stack = require_merged_prefix(dissolvable[0] if dissolvable else overlapping[0])
    unselected = tuple(number for number in stack.active_pr_numbers if number not in selected)
    if unselected:
        raise CliError(
            t"GitHub stack #{stack.number} also includes "
            t"{ui.join(lambda number: format_pr_number(number, repo=repo), unselected)}, "
            t"outside the selected stack.",
            hint=t"Select the complete stack, or run "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')}, then retry.",
        )
    return stack


async def dissolve_github_stack(
    *,
    github_client: GithubClient,
    stack: GithubStack,
) -> None:
    """Dissolve an observed stack and reject an incomplete mutation result."""

    try:
        remaining = await github_client.unstack(stack_number=stack.number)
    except GithubClientError as error:
        if error.status_code == 422:
            raise CliError(
                t"GitHub could not remove any pull requests from stack #{stack.number}.",
                hint=t"Resolve its locked pull requests, then retry "
                t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')}.",
            ) from None
        raise CliError(t"Could not remove GitHub stack #{stack.number}.") from error
    if remaining is not None and remaining.active_pr_numbers:
        members = ui.join(
            lambda number: format_pr_number(number, repo=github_client.repo),
            remaining.active_pr_numbers,
        )
        raise CliError(
            t"GitHub stack #{stack.number} still contains {members}.",
            hint=t"Resolve its locked pull requests, then retry "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')}.",
        )
