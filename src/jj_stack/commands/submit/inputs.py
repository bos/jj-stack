"""Load local submit state and run preflight checks before any GitHub mutation."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ConflictedStackError, UsageError
from jj_stack.github.resolution import select_submit_remote
from jj_stack.models.review_state import ReviewState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import resolve_review_branches
from jj_stack.review.selected import require_reviewable_revisions, select_review_path

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
    state: ReviewState,
) -> PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = context.jj_client
    remote = select_submit_remote(client.list_git_remotes())
    stack = select_review_path(
        jj_client=client,
        revset=options.revset,
        state=state,
    ).stack
    require_reviewable_revisions(stack.revisions)
    for revision in stack.revisions:
        if state.issues_for(revision.change_id):
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is malformed.",
                hint=t"Repair it with {ui.cmd('relink')} before submitting the review.",
            )
    branch_resolutions = resolve_review_branches(
        revisions=stack.revisions,
        review_identities=state.review_identities,
    )
    preflight_conflicted_revisions(stack.revisions)
    preflight_private_commits(client, stack.revisions)
    if options.edit and options.describe_with is not None:
        raise UsageError(t"Use either {ui.cmd('--edit')} or {ui.cmd('--describe-with')}.")
    (
        generated_pull_request_descriptions,
        generated_stack_description,
    ) = resolve_generated_descriptions(
        descriptions=options.descriptions,
        describe_with=options.describe_with,
        jj_client=client,
        selected_revset=stack.selected_revset,
        revisions=stack.revisions,
    )
    return PreparedSubmitInputs(
        branch_resolutions=branch_resolutions,
        client=client,
        generated_pull_request_descriptions=generated_pull_request_descriptions,
        generated_stack_description=generated_stack_description,
        remote=remote,
        stack=stack,
        state=state,
    )


def preflight_private_commits(
    client: PrivateCommitFinder,
    revisions: tuple[LocalRevision, ...],
) -> None:
    private = client.find_private_commits(revisions)
    if not private:
        return
    subjects = ui.join(
        lambda revision: t"{ui.change_id(revision.change_id)} ({revision.subject})",
        private,
    )
    raise CliError(
        t"Stack contains commits blocked by {ui.code('git.private-commits')}: {subjects}.",
        hint="Remove these changes from the stack before submitting.",
    )


def preflight_conflicted_revisions(revisions: tuple[LocalRevision, ...]) -> None:
    conflicted = tuple(revision for revision in revisions if revision.conflict)
    if not conflicted:
        return
    subjects = ui.join(
        lambda revision: t"{ui.change_id(revision.change_id)} ({revision.subject})",
        conflicted,
    )
    raise ConflictedStackError(
        t"Stack contains changes with unresolved conflicts: {subjects}. "
        t"Resolve these changes before submitting."
    )
