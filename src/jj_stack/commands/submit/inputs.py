"""Load local submit state and run preflight checks before any GitHub mutation."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ConflictedStackError, UsageError
from jj_stack.github.resolution import select_submit_remote
from jj_stack.jj.client import JjClient
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.pr_branches import resolve_pr_branches
from jj_stack.stack.selected import require_submittable_changes, select_stack_path

from .descriptions import resolve_generated_descriptions
from .models import (
    PreparedSubmitInputs,
    PrivateCommitFinder,
    SubmitOptions,
)


def prepare_submit_inputs(
    *,
    context: CommandContext,
    options: SubmitOptions,
    state: TrackingState,
) -> PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = context.jj_client
    remote = select_submit_remote(client.list_git_remotes())
    path = select_stack_path(
        jj_client=client,
        revset=options.revset,
        state=state,
    )
    stack = _select_submit_stack(
        base_revset=options.base_revset,
        jj_client=client,
        stack=path.stack,
        state=state,
    )
    if options.base_revset is not None:
        base = stack.base_parent
        retry = ui.cmd(f"jj-stack submit --base {base.change_id} {stack.head.change_id}")
        tracked_base = state.tracked_pr(base.change_id)
        if tracked_base is None:
            raise CliError(
                t"Base {ui.change_id(base.change_id)} has no submitted PR.",
                hint=t"Inspect the parent with {ui.cmd(f'jj-stack view {base.change_id}')}, "
                t"submit it using its usual submit command, then run {retry}.",
            )
        if tracked_base.submitted_baseline.commit_id != base.commit_id:
            raise CliError(
                t"Base {ui.change_id(base.change_id)} has changed since its last submit.",
                hint=t"Inspect the parent with {ui.cmd(f'jj-stack view {base.change_id}')}, "
                t"refresh it using its usual submit command, "
                t"then run {retry}.",
            )
    require_submittable_changes(stack.changes)
    branch_resolutions = resolve_pr_branches(
        changes=stack.changes,
        pr_identities=state.pr_identities,
    )
    preflight_conflicted_changes(stack.changes)
    preflight_private_commits(client, stack.changes)
    if options.edit and options.describe_with is not None:
        raise UsageError(t"Use either {ui.cmd('--edit')} or {ui.cmd('--describe-with')}.")
    (
        generated_pr_descriptions,
        generated_stack_description,
    ) = resolve_generated_descriptions(
        descriptions=options.descriptions,
        describe_with=options.describe_with,
        jj_client=client,
        selected_revset=stack.selected_revset,
        changes=stack.changes,
    )
    return PreparedSubmitInputs(
        branch_resolutions=branch_resolutions,
        client=client,
        generated_pr_descriptions=generated_pr_descriptions,
        generated_stack_description=generated_stack_description,
        is_maximal_path=path.is_maximal,
        remote=remote,
        stack=stack,
        state=state,
    )


def _select_submit_stack(
    *,
    base_revset: str | None,
    jj_client: JjClient,
    stack: LocalStack,
    state: TrackingState,
) -> LocalStack:
    """Select the ordinary path, optionally excluding one explicit submitted ancestor."""

    if base_revset is None:
        return stack
    base = select_stack_path(
        jj_client=jj_client,
        revset=base_revset,
        state=state,
    ).stack.head
    if base.commit_id == stack.base_parent.commit_id:
        base_index = -1
    else:
        base_index = next(
            (
                index
                for index, change in enumerate(stack.changes)
                if change.commit_id == base.commit_id
            ),
            None,
        )
    if base_index is None:
        raise CliError(
            t"Base {ui.revset(base_revset)} is not an ancestor of the selected head on its "
            t"single-parent path.",
            hint=t"Choose the submitted parent immediately below the changes to submit.",
        )
    changes = stack.changes[base_index + 1 :]
    if not changes:
        raise CliError(
            t"Base {ui.revset(base_revset)} is the selected head, so there are no child "
            t"changes to submit."
        )
    return stack.model_copy(
        update={
            "base_parent": base,
            "changes": changes,
            "selected_revset": f"{base.commit_id}..{stack.head.commit_id}",
        }
    )


def preflight_private_commits(
    client: PrivateCommitFinder,
    changes: tuple[LocalCommit, ...],
) -> None:
    private = client.find_private_commits(changes)
    if not private:
        return
    subjects = ui.join(
        lambda change: t"{ui.change_id(change.change_id)} ({change.subject})",
        private,
    )
    raise CliError(
        t"Stack contains changes blocked by {ui.code('git.private-commits')}: {subjects}.",
        hint="Remove these changes from the stack before submitting.",
    )


def preflight_conflicted_changes(changes: tuple[LocalCommit, ...]) -> None:
    conflicted = tuple(change for change in changes if change.conflict)
    if not conflicted:
        return
    subjects = ui.join(
        lambda change: t"{ui.change_id(change.change_id)} ({change.subject})",
        conflicted,
    )
    raise ConflictedStackError(
        t"Stack contains changes with unresolved conflicts: {subjects}. "
        t"Resolve these changes before submitting."
    )
