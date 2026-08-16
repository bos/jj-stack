"""Recover an exact native GitHub stack rebase without replacing jj change identities."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.jj.client import ReviewRefUpdate
from jj_stack.models.review_state import SubmittedBaseline
from jj_stack.models.stack import LocalRevision
from jj_stack.review.convergence import SelectedConvergencePlan
from jj_stack.review.github_stack_sync import GithubStackRewrite


def recover_github_stack_rebase(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: SelectedConvergencePlan,
    remote_name: str,
    rewrite: GithubStackRewrite,
    trunk_commit_id: str,
) -> int:
    """Verify GitHub's rewritten trees, then restore the original local change IDs."""

    active = rewrite.active
    if len(active) > len(plan.survivors) or any(
        remote.candidate.change_id != local.change_id
        for remote, local in zip(active, plan.survivors, strict=False)
    ):
        raise AssertionError("GitHub rebase recovery requires the complete reviewed prefix.")
    if dry_run:
        return 0

    top = active[-1]
    with context.jj_client.import_remote_review_ref(
        remote=remote_name,
        branch=top.candidate.review_identity.head_ref,
        namespace=context.review_namespace,
        expected_target=top.remote_head_commit_id,
        expected_chain=tuple(
            (
                item.candidate.review_identity.head_ref,
                item.remote_head_commit_id,
                (None, item.candidate.change_id),
            )
            for item in active
        ),
        expected_parent_commit_id=trunk_commit_id,
    ):
        desired, operation_id = _verified_local_rebase(
            context=context,
            local=plan.survivors,
            remote=rewrite,
            trunk_commit_id=trunk_commit_id,
        )
        if operation_id is not None:
            context.jj_client.integrate_operation(operation_id)
        reviewed = desired[: len(active)]
        context.jj_client.mutate_remote_review_refs(
            namespace=context.review_namespace,
            remote=remote_name,
            updates=tuple(
                ReviewRefUpdate(
                    branch=item.candidate.review_identity.head_ref,
                    expected_target=item.remote_head_commit_id,
                    desired_target=revision.commit_id,
                )
                for item, revision in zip(active, reviewed, strict=True)
            ),
        )
        context.state_store.relink_reviews(
            replacements={
                item.candidate.change_id: (
                    item.candidate.review_identity,
                    SubmittedBaseline(commit_id=revision.commit_id),
                )
                for item, revision in zip(active, reviewed, strict=True)
            },
        )
    return 0


def _verified_local_rebase(
    *,
    context: CommandContext,
    local: tuple[LocalRevision, ...],
    remote: GithubStackRewrite,
    trunk_commit_id: str,
) -> tuple[tuple[LocalRevision, ...], str | None]:
    active = remote.active
    baselines = tuple(item.candidate.submitted_baseline.commit_id for item in active)
    if tuple(revision.commit_id for revision in local[: len(active)]) == baselines:
        operation_id = context.jj_client.prepare_rebase_revisions_only(
            revisions=tuple(revision.commit_id for revision in local),
            destination=trunk_commit_id,
        )
        grouped = context.jj_client.query_revisions_at_operation(
            change_ids=tuple(revision.change_id for revision in local),
            operation_id=operation_id,
        )
        desired = tuple(
            revisions[0]
            for revision in local
            if len(revisions := grouped[revision.change_id]) == 1
        )
        if len(desired) != len(local):
            raise CliError(
                "The local changes did not have one exact result after rebasing onto trunk."
            )
    else:
        desired = local
        operation_id = None

    _require_linear_result(desired, trunk_commit_id=trunk_commit_id)
    _require_matching_trees(context=context, desired=desired, remote=remote)
    return desired, operation_id


def _require_linear_result(
    revisions: tuple[LocalRevision, ...],
    *,
    trunk_commit_id: str,
) -> None:
    expected_parent = trunk_commit_id
    for revision in revisions:
        if revision.conflict:
            raise CliError(
                t"Rebasing {ui.change_id(revision.change_id)} locally produced conflicts.",
                hint=t"Rebase and resolve the stack with {ui.cmd('jj')}, then run "
                t"{ui.cmd('jj-stack submit')}.",
            )
        if revision.parents != (expected_parent,):
            raise CliError(
                "The local stack does not match GitHub's rebase onto fetched trunk.",
                hint=t"Inspect the local and GitHub stacks, then restore or resubmit the "
                t"intended review.",
            )
        expected_parent = revision.commit_id


def _require_matching_trees(
    *,
    context: CommandContext,
    desired: tuple[LocalRevision, ...],
    remote: GithubStackRewrite,
) -> None:
    desired_ids = tuple(revision.commit_id for revision in desired)
    remote_ids = tuple(item.remote_head_commit_id for item in remote.active)
    trees = context.jj_client.git_tree_ids((*desired_ids, *remote_ids))
    if any(
        trees[desired_id] != trees[remote_id]
        for desired_id, remote_id in zip(desired_ids[: len(remote_ids)], remote_ids, strict=True)
    ):
        raise CliError(
            "GitHub's rewritten stack does not have the same contents as the local rebase.",
            hint=t"Inspect the changed review branches on GitHub before choosing which version "
            t"to keep.",
        )
