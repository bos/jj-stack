"""Plan and publish pull request updates for submit and sync."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_stack.errors import CliError, error_message
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.identifiers import CommitId
from jj_stack.jj.client import PRRefUpdate
from jj_stack.models.github import GithubStack
from jj_stack.stack.github_stack_safety import dissolve_github_stack

from . import auto_close
from .auto_close import retarget_pr_bases_before_branch_push
from .github_stack import (
    apply_github_stack_plan,
    omitted_active_stack_prs,
    plan_github_stack,
)
from .inputs import confirm_orphaned_pr_snapshots
from .models import (
    GeneratedDescription,
    PreparedSubmitChange,
    PRMetadataAction,
    PRSyncPlan,
    PublicationInputs,
    SubmitMutationRun,
)
from .overview_comments import plan_stack_overview, sync_stack_overview_comments
from .prs import sync_prs
from .render import print_submit_preview, print_submitted_changes
from .revision_comments import (
    REVISION_HISTORY_COMMENT_MARKER,
    REVISION_HISTORY_VERSION_LIMIT,
    sync_revision_history_comments,
)


def plan_pr_updates(
    *,
    bottom_base_branch: str,
    drafts: dict[str, bool],
    generated_descriptions: dict[str, GeneratedDescription],
    metadata: PRMetadataAction,
    explicit_metadata: bool = False,
    prepared_changes: tuple[PreparedSubmitChange, ...],
    prior_reviewers: Mapping[int, list[str]],
) -> tuple[PRSyncPlan, ...]:
    labels, reviewers, team_reviewers = metadata
    base_branches = (
        bottom_base_branch,
        *(change.branch for change in prepared_changes[:-1]),
    )
    plans: list[PRSyncPlan] = []
    for prepared, base_branch in zip(prepared_changes, base_branches, strict=True):
        pr = prepared.pr
        plan = PRSyncPlan(
            base_branch=base_branch,
            draft=drafts[prepared.change.change_id],
            generated_description=generated_descriptions[prepared.change.change_id],
            metadata=None,
            prepared=prepared,
        )
        prior = prior_reviewers.get(pr.number, ()) if pr else ()
        merged_reviewers = list(dict.fromkeys((*reviewers, *prior)))
        full_metadata = plan.action != "unchanged" or explicit_metadata
        if full_metadata or merged_reviewers != reviewers:
            plan = replace(
                plan,
                metadata=PRMetadataAction(
                    labels=labels if full_metadata else [],
                    reviewers=merged_reviewers,
                    team_reviewers=team_reviewers if full_metadata else [],
                ),
            )
        plans.append(plan)
    return tuple(plans)


async def publish_prepared(
    *,
    context: CommandContext,
    github_client: GithubClient,
    prepared_inputs: PublicationInputs,
    pr_plans: tuple[PRSyncPlan, ...],
    remote_targets: dict[str, CommitId],
    retry_hint: ui.Message,
    observed_stacks: tuple[GithubStack, ...],
    trunk_branch: str,
    trunk_targets: dict[str, CommitId],
    dry_run: bool,
) -> None:
    client = prepared_inputs.client
    state = prepared_inputs.state
    prepared_changes = tuple(plan.prepared for plan in pr_plans)
    pushes_pr_branches = any(change.remote_action == "pushed" for change in prepared_changes)
    planned_branches = {change.branch for change in prepared_changes}
    observed_base_refs = tuple(
        dict.fromkeys(
            pr.base.ref
            for plan in pr_plans
            if (pr := plan.prepared.pr) is not None
            and pr.state == "open"
            and pr.base.ref not in planned_branches
            and pr.base.ref not in trunk_targets
            and pr.base.ref not in remote_targets
        )
    )
    observed_base_targets = await github_client.get_branch_targets(
        branches=observed_base_refs,
    )
    retarget_prs = (
        auto_close.predict_prs_auto_closed_by_push(
            jj_client=client,
            plans=pr_plans,
            prepared_changes=prepared_changes,
            remote_targets={**trunk_targets, **remote_targets, **observed_base_targets},
        )
        if pushes_pr_branches
        else ()
    )
    desired_pr_numbers = tuple(
        plan.prepared.pr.number if plan.prepared.pr is not None else None for plan in pr_plans
    )
    omitted_stack_prs = (
        omitted_active_stack_prs(
            desired=desired_pr_numbers,
            observed_stacks=observed_stacks,
        )
        if not prepared_inputs.is_maximal_path
        else ()
    )
    orphaned_pr_snapshots = confirm_orphaned_pr_snapshots(
        candidates=omitted_stack_prs,
        jj_client=client,
        state=state,
    )
    github_stack_plan = plan_github_stack(
        desired=desired_pr_numbers,
        is_maximal_path=prepared_inputs.is_maximal_path,
        observed_stacks=observed_stacks,
        orphaned_pr_snapshots=orphaned_pr_snapshots,
        pr_numbers_requiring_base_update={
            pr.number
            for plan in pr_plans
            if (pr := plan.prepared.pr) is not None
            and (pr.base.ref != plan.base_branch or pr in retarget_prs)
        },
        repo=github_client.repo,
    )
    stacks_to_dissolve = (
        github_stack_plan.affected_stacks if github_stack_plan.action == "replace" else ()
    )
    pr_branch_ref_updates = tuple(
        PRRefUpdate(
            branch=prepared.branch,
            expected_target=prepared.expected_remote_target,
            desired_target=prepared.change.commit_id,
        )
        for prepared in prepared_changes
    )

    with console.spinner(description="Loading pull request comments"):
        try:
            (
                comments_by_marker,
                revisions_by_pr,
            ) = await github_client.find_issue_comments_and_revisions(
                body_markers=(
                    STACK_OVERVIEW_COMMENT_MARKER,
                    REVISION_HISTORY_COMMENT_MARKER,
                ),
                pr_numbers=tuple(number for number in desired_pr_numbers if number is not None),
                revision_limit=REVISION_HISTORY_VERSION_LIMIT,
            )
        except GithubClientError as error:
            raise CliError("Could not load pull request comments", hint=retry_hint) from error
    overview_comments = comments_by_marker[STACK_OVERVIEW_COMMENT_MARKER]
    try:
        overview_body = plan_stack_overview(
            comments=tuple(
                overview_comments.get(number) if number is not None else None
                for number in desired_pr_numbers
            ),
            generated_stack_description=prepared_inputs.generated_stack_description,
            # A single selected PR based on another PR is still part of a larger stack.
            is_lone_pr=len(pr_plans) == 1 and pr_plans[0].base_branch == trunk_branch,
        )
    except CliError as error:
        raise CliError(
            error_message(error),
            hint=(error.hint, " ", retry_hint) if error.hint is not None else retry_hint,
        ) from error

    if dry_run:
        print_submit_preview(
            inputs=prepared_inputs,
            plans=pr_plans,
            github_stack_plan=github_stack_plan,
        )
        return
    for github_stack in stacks_to_dissolve:
        await dissolve_github_stack(github_client=github_client, stack=github_stack)
    # GitHub has no transaction spanning PR branches, pull requests, and stack
    # membership. An external stack edit can race this mutation, and submit accepts that
    # narrow window rather than pretending another non-atomic observation closes it.
    if retarget_prs:
        await retarget_pr_bases_before_branch_push(
            github_client=github_client,
            prs=retarget_prs,
            trunk_branch=trunk_branch,
        )
    with console.spinner(description="Pushing PR branches"):
        prepared_inputs.client.mutate_remote_pr_branch_refs(
            remote=prepared_inputs.remote.name,
            updates=pr_branch_ref_updates,
        )
    with console.progress(
        description="Syncing pull requests",
        total=len(pr_plans),
    ) as progress:
        submitted = await sync_prs(
            github_client=github_client,
            on_progress=progress.advance,
            plans=pr_plans,
            run=SubmitMutationRun(state=state, state_store=context.state_store),
        )
    pr_numbers = tuple(pr.number for _, pr in submitted)
    submitted_force_pushes_by_pr = {
        pr.number: (expected_target, plan.prepared.change.commit_id)
        for plan, pr in submitted
        if plan.action != "created"
        and plan.prepared.remote_action == "pushed"
        and (expected_target := plan.prepared.expected_remote_target) is not None
    }
    try:
        grouped = await apply_github_stack_plan(
            github_client=github_client,
            plan=github_stack_plan,
            pr_numbers=pr_numbers,
        )
        await sync_stack_overview_comments(
            comments_by_pr_number=overview_comments,
            concurrency=DEFAULT_BOUNDED_CONCURRENCY,
            overview_body=overview_body,
            github_client=github_client,
            pr_numbers=pr_numbers,
        )
        await sync_revision_history_comments(
            comments_by_pr_number=comments_by_marker[REVISION_HISTORY_COMMENT_MARKER],
            concurrency=DEFAULT_BOUNDED_CONCURRENCY,
            github_client=github_client,
            pr_numbers=pr_numbers,
            revisions_by_pr=revisions_by_pr,
            submitted_force_pushes_by_pr=submitted_force_pushes_by_pr,
        )
    except CliError as error:
        published = ui.join(
            lambda pr: format_pr_label(pr.number, url=pr.html_url),
            tuple(pr for _, pr in submitted),
        )
        raise CliError(
            (t"Published {published}. ", error_message(error)),
            hint=(error.hint, " ", retry_hint) if error.hint is not None else retry_hint,
        ) from error
    print_submitted_changes(inputs=prepared_inputs, changes=submitted)
    actions = [f"dissolved GitHub stack #{stack.number}" for stack in stacks_to_dissolve]
    if grouped is not None:
        verb = "extended" if github_stack_plan.action == "append" else "created"
        actions.append(f"{verb} GitHub stack #{grouped.number}")
    if actions:
        summary = ", ".join(actions)
        console.output(f"{summary[0].upper()}{summary[1:]}.")
