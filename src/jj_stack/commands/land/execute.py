"""Live execution for the land command.

Execution is mutate-only: gates were checked during planning, and every
recovery path is observational. The direct-push transport moves trunk with one
leased push and then finalizes the planned landed reviews; the
merge transport asks GitHub to merge the planned prefix bottom-up and reports
exactly what GitHub accepted so the caller can converge the local stack. No
durable transaction state exists: an interruption at any point is recovered by
the next `land` or `sync` from what GitHub and the jj DAG report then.
"""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.push_rejections import (
    classify_protected_branch_rejection,
    protected_branch_rejection_hint,
    rejection_reason_lines,
)
from jj_stack.jj.client import JjClient, JjCommandError
from jj_stack.review.landed import (
    FinalizationContext,
    LandedReviewResult,
    finalize_landed_reviews,
    retire_landed_reviews,
)
from jj_stack.review.landed_evidence import (
    candidate_for_change,
)
from jj_stack.review.observation import observe_review_mutation

from .authority import land_authority_error
from .models import (
    LandAction,
    LandExecutionInputs,
    LandPlan,
    LandResult,
    LandRevision,
)
from .pull_requests import merge_landed_pull_request


async def execute_land_plan(
    *,
    github_client: GithubClient,
    merge_method: str | None,
    plan: LandPlan,
    execution: LandExecutionInputs,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_commit_id: str,
    trunk_subject: str,
) -> LandResult:
    client = execution.context.jj_client
    state_store = execution.context.state_store

    def land_result(
        *,
        actions: tuple[LandAction, ...],
        applied: bool,
        blocked: bool,
        merged_change_ids: tuple[str, ...] = (),
    ) -> LandResult:
        return LandResult(
            actions=actions,
            applied=applied,
            blocked=blocked,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
            via=plan.via,
            merged_change_ids=merged_change_ids,
        )

    if plan.blocked:
        return land_result(
            actions=plan.planned_actions(),
            applied=False,
            blocked=True,
        )
    state_store.require_writable()

    actions: list[LandAction] = []

    if plan.via == "push":
        return await _execute_direct_push(
            actions=actions,
            client=client,
            execution=execution,
            github_client=github_client,
            land_result=land_result,
            plan=plan,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk_commit_id,
        )
    return await _execute_github_merges(
        actions=actions,
        execution=execution,
        github_client=github_client,
        land_result=land_result,
        merge_method=merge_method,
        plan=plan,
        remote_name=remote_name,
        selected_revset=selected_revset,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_commit_id,
    )


async def _execute_direct_push(
    *,
    actions: list[LandAction],
    client: JjClient,
    execution: LandExecutionInputs,
    github_client: GithubClient,
    land_result,  # noqa: ANN001 - local result factory
    plan: LandPlan,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> LandResult:
    try:
        observation = await observe_review_mutation(
            change_ids=tuple(revision.change_id for revision in plan.planned_revisions),
            context=execution.context,
            github_client=github_client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
        )
    except (CliError, GithubClientError, JjCommandError) as error:
        return land_result(
            actions=(_freshness_boundary(plan.planned_revisions[0], str(error)),),
            applied=False,
            blocked=True,
        )
    authority_error = land_authority_error(
        bypass_readiness=execution.bypass_readiness,
        expected_bases={
            revision.change_id: (revision.base_ref,) for revision in plan.planned_revisions
        },
        expected_repository=github_client.repository,
        expected_trunk_branch=trunk_branch,
        expected_trunk_commit_id=trunk_commit_id,
        observation=observation,
        remote_name=remote_name,
        revisions=plan.planned_revisions,
    )
    if authority_error is not None:
        return land_result(
            actions=(_freshness_boundary(plan.planned_revisions[0], authority_error),),
            applied=False,
            blocked=True,
        )
    if observation.remote is None or observation.remote_trunk_target is None:
        raise AssertionError("Authorized direct push requires a live remote trunk target.")

    await execution.native_stacks.require_unstacked()
    trunk_revision = plan.planned_revisions[-1]
    trunk_action = _push_trunk_bookmark(
        client=client,
        expected_remote_target=observation.remote_trunk_target,
        remote_name=remote_name,
        remote_target=observation.remote.push_url,
        trunk_branch=trunk_branch,
        trunk_revision=trunk_revision,
    )
    actions.append(trunk_action)

    subjects = {revision.change_id: revision.subject for revision in plan.planned_revisions}
    state = execution.context.state_store.load()
    candidates = tuple(
        candidate
        for revision in plan.planned_revisions
        if (candidate := candidate_for_change(state, revision.change_id)) is not None
    )
    if len(candidates) != len(plan.planned_revisions):
        raise AssertionError("Authorized direct landing requires complete review state.")
    finalizer = FinalizationContext(
        command=execution.context,
        dry_run=False,
        github=github_client,
        remote_name=remote_name,
        trunk_branch=trunk_branch,
        trunk_commit_id=trunk_revision.commit_id,
    )
    landed_results = await finalize_landed_reviews(
        candidates=candidates,
        finalizer=finalizer,
        labels=subjects,
    )
    landed_results = await retire_landed_reviews(
        cleanup_bookmarks=execution.cleanup_bookmarks,
        evidence={candidate.change_id: "exact" for candidate in candidates},
        finalization_results=landed_results,
        finalizer=finalizer,
    )
    landed_actions, any_skipped = render_landed_actions(
        results=landed_results,
        subjects=subjects,
    )
    actions.extend(landed_actions)
    if any_skipped:
        return land_result(actions=tuple(actions), applied=True, blocked=True)
    completed_actions = tuple(actions)
    if plan.boundary_action is not None:
        completed_actions = (*completed_actions, plan.boundary_action)
    return land_result(
        actions=completed_actions,
        applied=True,
        blocked=False,
    )


async def _execute_github_merges(
    *,
    actions: list[LandAction],
    execution: LandExecutionInputs,
    github_client: GithubClient,
    land_result,  # noqa: ANN001 - local result factory
    merge_method: str | None,
    plan: LandPlan,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> LandResult:
    if merge_method is None:
        raise AssertionError("The merge transport requires a resolved merge method.")
    await execution.native_stacks.require_unstacked()
    merged_change_ids: list[str] = []
    blocked_action: LandAction | None = None
    current_trunk_commit_id = trunk_commit_id
    for landed_revision in plan.planned_revisions:
        console.output(
            t"Merging PR #{landed_revision.identity.pr_number} for "
            t"{landed_revision.subject} "
            t"{ui.change_id(landed_revision.change_id)}..."
        )
        final_pull_request, blocked = await merge_landed_pull_request(
            bypass_readiness=execution.bypass_readiness,
            context=execution.context,
            github_client=github_client,
            landed_revision=landed_revision,
            merge_method=merge_method,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            trunk_commit_id=current_trunk_commit_id,
        )
        if blocked is not None or final_pull_request is None:
            blocked_action = blocked
            break
        actions.append(
            LandAction(
                kind="pull request",
                body=t"merge PR #{landed_revision.identity.pr_number} into "
                t"{ui.bookmark(trunk_branch)} on GitHub for "
                t"{landed_revision.subject} "
                t"{ui.change_id(landed_revision.change_id)}",
                status="applied",
            )
        )
        merged_change_ids.append(landed_revision.change_id)
        try:
            execution.context.jj_client.fetch_remote(
                remote=remote_name,
                branches=(trunk_branch,),
            )
            current_trunk_commit_id = execution.context.jj_client.resolve_revision(
                "trunk()"
            ).commit_id
        except (CliError, JjCommandError) as error:
            blocked_action = LandAction(
                kind="boundary",
                body=t"after accepted {ui.change_id(landed_revision.change_id)}: could not "
                t"refresh trunk: {error}; rerun {ui.cmd(f'sync {selected_revset}')}",
                status="blocked",
            )
            break
    if blocked_action is not None:
        actions.append(blocked_action)
    blocked = blocked_action is not None or len(merged_change_ids) != len(plan.planned_revisions)
    completed_actions = tuple(actions)
    if not blocked and plan.boundary_action is not None:
        completed_actions = (*completed_actions, plan.boundary_action)
    return land_result(
        actions=completed_actions,
        applied=True,
        blocked=blocked,
        merged_change_ids=tuple(merged_change_ids),
    )


def render_landed_actions(
    *,
    results: tuple[LandedReviewResult, ...],
    subjects: dict[str, str] | None = None,
) -> tuple[tuple[LandAction, ...], bool]:
    labels = subjects or {}
    actions: list[LandAction] = []
    any_skipped = False
    for result in results:
        candidate = result.candidate
        subject = labels.get(candidate.change_id)
        label = (
            t"{subject} {ui.change_id(candidate.change_id)}"
            if subject is not None
            else t"{ui.change_id(candidate.change_id)}"
        )
        if result.outcome == "skipped":
            any_skipped = True
            actions.append(
                LandAction(
                    kind="pull request",
                    body=t"could not finish PR cleanup for landed {label}: "
                    t"{result.skip_reason}; "
                    t"rerun {ui.cmd('jj-stack sync --all')}",
                    status="blocked",
                )
            )
            continue
        if result.outcome == "finalized":
            actions.append(
                LandAction(
                    kind="pull request",
                    body=t"finish landed PR #{candidate.review_identity.pr_number} for {label}",
                    status="applied",
                )
            )
        if result.forgot_bookmark:
            actions.append(
                LandAction(
                    kind="local bookmark",
                    body=t"forget {ui.bookmark(candidate.review_identity.head_ref)} for {label}",
                    status="applied",
                )
            )
        if result.cleanup_warning is not None:
            console.warning(t"Cleanup still needed for landed {label}: {result.cleanup_warning}")
        if result.retired_tracking:
            actions.append(
                LandAction(
                    kind="tracking",
                    body=t"remove tracking for landed {label}",
                    status="applied",
                )
            )
        elif result.retirement_skip_reason is not None:
            actions.append(
                LandAction(
                    kind="tracking",
                    body=t"keep tracking for landed {label}: {result.retirement_skip_reason}",
                    status="blocked",
                )
            )
    return tuple(actions), any_skipped


def _push_trunk_bookmark(
    *,
    client: JjClient,
    expected_remote_target: str,
    remote_name: str,
    remote_target: str,
    trunk_branch: str,
    trunk_revision: LandRevision,
) -> LandAction:
    push_error: JjCommandError | None = None
    try:
        client.push_bookmark_with_lease(
            remote_target=remote_target,
            bookmark=trunk_branch,
            desired_target=trunk_revision.commit_id,
            expected_remote_target=expected_remote_target,
        )
    except JjCommandError as error:
        push_error = error
    try:
        client.fetch_remote(remote=remote_name, branches=(trunk_branch,))
        remote_commit = client.resolve_revision(f"{trunk_branch}@{remote_name}").commit_id
    except (CliError, JjCommandError) as error:
        raise CliError(
            "The trunk push may have succeeded, but its remote result could not be refreshed.",
            hint=t"Inspect trunk, then run {ui.cmd('sync --all')} to clean up landed PRs.",
        ) from error
    if push_error is not None and remote_commit != trunk_revision.commit_id:
        rejection_reason = classify_protected_branch_rejection(str(push_error))
        if rejection_reason is None:
            raise push_error
        raise CliError(
            t"GitHub rejected the {ui.bookmark(trunk_branch)} push as a "
            t"protected-branch violation:\n"
            t"{rejection_reason_lines(str(push_error))}",
            hint=protected_branch_rejection_hint(rejection_reason),
        ) from push_error
    return LandAction(
        kind="trunk",
        body=t"push {ui.bookmark(trunk_branch)} to "
        t"{trunk_revision.subject} "
        t"{ui.change_id(trunk_revision.change_id)}",
        status="applied",
    )


def _freshness_boundary(revision: LandRevision, reason: str) -> LandAction:
    return LandAction(
        kind="boundary",
        body=t"before {revision.subject} {ui.change_id(revision.change_id)} because "
        t"{reason}; inspect the changed repository or PR state and rerun {ui.cmd('land')}",
        status="blocked",
    )
