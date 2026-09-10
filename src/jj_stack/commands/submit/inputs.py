"""Load local submit state and run preflight checks before any GitHub mutation."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ConflictedStackError, UsageError
from jj_stack.github.resolution import select_submit_remote
from jj_stack.identifiers import short_change_id
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubStackPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.observation import observe_change_copies
from jj_stack.stack.selected import require_submittable_changes, select_stack_path

from .descriptions import read_pr_template, resolve_generated_descriptions
from .github_stack import GithubStackPRSnapshot, github_stack_pr_snapshot
from .models import (
    ExplicitBase,
    PrivateCommitFinder,
    PublicationInputs,
    SubmitOptions,
)


def prepare_submit_inputs(
    *,
    context: CommandContext,
    options: SubmitOptions,
    state: TrackingState,
) -> PublicationInputs:
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
    explicit_base = None
    if options.base_revset is not None:
        base = stack.base_parent
        short_base = short_change_id(base.change_id)
        short_head = short_change_id(stack.head.change_id)
        if base.commit_id == stack.trunk.commit_id:
            raise CliError(
                t"Base {ui.revset(options.base_revset)} is the trunk commit, which submit "
                t"already uses as the base of the whole stack.",
                hint=t"Run {ui.cmd(f'jj-stack submit {short_head}')} without {ui.cmd('--base')}.",
            )
        retry = ui.cmd(f"jj-stack submit --base {short_base} {short_head}")
        tracked_base = state.prs.get(base.change_id)
        if tracked_base is None:
            raise CliError(
                t"Base {ui.change_id(base.change_id)} has no submitted PR.",
                hint=t"Inspect the parent with {ui.cmd(f'jj-stack view {short_base}')}, "
                t"submit it using its usual submit command, then run {retry}.",
            )
        if tracked_base.submitted_baseline.commit_id != base.commit_id:
            raise CliError(
                t"Base {ui.change_id(base.change_id)} has changed since its last submit.",
                hint=t"Inspect the parent with {ui.cmd(f'jj-stack view {short_base}')}, "
                t"refresh it using its usual submit command, "
                t"then run {retry}.",
            )
        explicit_base = ExplicitBase(change=base, tracked=tracked_base)
    if options.edit and options.describe_with is not None:
        raise UsageError(
            t"{ui.cmd('--describe-with')} cannot be combined with {ui.cmd('--edit')} or "
            t"{ui.cmd('--resume-edit')}."
        )
    return prepare_publication_inputs(
        context=context,
        stack=stack,
        remote=remote,
        state=state,
        is_maximal_path=path.is_maximal,
        descriptions=options.descriptions,
        describe_with=options.describe_with,
        explicit_base=explicit_base,
    )


def prepare_publication_inputs(
    *,
    context: CommandContext,
    stack: LocalStack,
    remote: GitRemote,
    state: TrackingState,
    is_maximal_path: bool,
    descriptions: tuple[str, ...] = (),
    describe_with: str | None = None,
    explicit_base: ExplicitBase | None = None,
) -> PublicationInputs:
    client = context.jj_client
    require_submittable_changes(stack.changes)
    preflight_conflicted_changes(stack.changes)
    preflight_private_commits(client, stack.changes)
    template = read_pr_template(client.repo_root)
    (
        generated_pr_descriptions,
        generated_stack_description,
    ) = resolve_generated_descriptions(
        descriptions=descriptions,
        describe_with=describe_with,
        jj_client=client,
        selected_revset=stack.selected_revset,
        changes=stack.changes,
        template=template,
    )
    submitted_commits = client.query_commits_by_ids(
        tuple(
            state.prs[change.change_id].submitted_baseline.commit_id
            for change in stack.changes
            if change.change_id in state.prs
        )
    )
    return PublicationInputs(
        client=client,
        explicit_base=explicit_base,
        generated_pr_descriptions=generated_pr_descriptions,
        generated_stack_description=generated_stack_description,
        is_maximal_path=is_maximal_path,
        pr_template=template,
        remote=remote,
        stack=stack,
        state=state,
        submitted_commits={change.change_id: change for change in submitted_commits},
    )


def confirm_orphaned_pr_snapshots(
    *,
    candidates: tuple[GithubStackPR, ...],
    jj_client: JjClient,
    state: TrackingState,
) -> frozenset[GithubStackPRSnapshot]:
    """Check saved PRs whose local changes have no visible copy outside trunk."""

    candidate_snapshots = {github_stack_pr_snapshot(candidate) for candidate in candidates}
    change_ids_by_snapshot: dict[GithubStackPRSnapshot, list[str]] = {}
    for change_id, tracked in sorted(state.prs.items()):
        # Do not add repository identity to this match. jj-stack operates on one configured
        # repository, and these candidates were observed through its GitHub client. PR number,
        # branch, and submitted commit are the values this check must compare.
        snapshot = (
            tracked.pr_identity.pr_number,
            tracked.pr_identity.head_ref,
            tracked.submitted_baseline.commit_id,
        )
        if snapshot in candidate_snapshots:
            change_ids_by_snapshot.setdefault(snapshot, []).append(change_id)
    if not change_ids_by_snapshot:
        return frozenset()

    change_ids = tuple(
        change_id
        for matching_change_ids in change_ids_by_snapshot.values()
        for change_id in matching_change_ids
    )
    off_trunk_copies = observe_change_copies(
        jj_client=jj_client, state=state, change_ids=change_ids
    ).copies(change_ids, off_trunk=True)
    return frozenset(
        snapshot
        for snapshot, matching_change_ids in change_ids_by_snapshot.items()
        if all(not off_trunk_copies[change_id] for change_id in matching_change_ids)
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
            t"Base {ui.revset(base_revset)} is not an ancestor of the selected head within "
            t"its stack.",
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
