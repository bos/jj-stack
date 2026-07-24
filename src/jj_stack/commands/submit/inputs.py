"""Load local submit state and run preflight checks before any GitHub mutation."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ConflictedStackError
from jj_stack.github.resolution import select_submit_remote
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import resolve_review_branches
from jj_stack.review.restart import RestartedReview, restart_state_for_stack

from .descriptions import resolve_generated_descriptions
from .models import (
    PreparedSubmitInputs,
    PrivateCommitFinder,
    ResolvedSubmitOptions,
    SubmitOptions,
)


def prepare_submit_inputs(
    *,
    context: CommandContext,
    options: SubmitOptions,
    resolved_options: ResolvedSubmitOptions,
) -> PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = context.jj_client
    state_store = context.state_store
    remote = select_submit_remote(client.list_git_remotes())
    stack = client.discover_review_stack(options.revset)
    state = state_store.load()
    for revision in stack.revisions:
        if state.issues_for(revision.change_id):
            raise CliError(
                t"Saved review state for {ui.change_id(revision.change_id)} is malformed.",
                hint=t"Repair it with {ui.cmd('relink')} before submitting the review.",
            )
    restarted_change_ids: frozenset[str] = frozenset()
    restarted_reviews: tuple[RestartedReview, ...] = ()
    forced_branches: dict[str, str] = {}
    if options.restart:
        restart_result = restart_state_for_stack(
            stack=stack,
            state=state,
        )
        state = restart_result.state
        restarted_reviews = restart_result.restarted
        restarted_change_ids = frozenset(
            restarted.change_id for restarted in restart_result.restarted
        )
        forced_branches = {
            restarted.change_id: restarted.new_branch for restarted in restart_result.restarted
        }
    branch_resolutions = resolve_review_branches(
        revisions=stack.revisions,
        review_identities=state.review_identities,
        overrides=forced_branches,
    )
    preflight_conflicted_revisions(stack.revisions)
    preflight_private_commits(client, stack.revisions)
    (
        generated_pull_request_descriptions,
        generated_stack_description,
    ) = resolve_generated_descriptions(
        descriptions=options.descriptions,
        describe_with=options.describe_with,
        edit=options.edit,
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
        restarted_change_ids=restarted_change_ids,
        restarted_reviews=restarted_reviews,
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
