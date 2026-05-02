"""Live execution for the land command."""

from __future__ import annotations

from jj_review import console, ui
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.models.github import GithubPullRequest
from jj_review.models.review_state import CachedChange
from jj_review.review.intents import retire_superseded_intents
from jj_review.review.status import normalize_pull_request_state
from jj_review.state.intents import save_intent, write_new_intent

from .models import (
    BookmarkRestorer,
    BookmarkStateReader,
    LandAction,
    LandPlan,
    LandResult,
    LandRevision,
    PreparedLand,
    ReviewBookmarkCleanupPlan,
)
from .plan import completed_land_actions, planned_land_actions
from .resume import CompletedLandResume, build_land_intent, prepare_land_execution_state


def restore_local_trunk_bookmark(
    *,
    client: BookmarkRestorer,
    original_target: str | None,
    trunk_branch: str,
) -> None:
    if original_target is None:
        client.forget_bookmarks((trunk_branch,))
        return
    client.set_bookmark(trunk_branch, original_target, allow_backwards=True)


def ensure_trunk_branch_matches_selected_trunk(
    *,
    client: BookmarkStateReader,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> None:
    bookmark_state = client.get_bookmark_state(trunk_branch)
    if len(bookmark_state.local_targets) > 1:
        raise CliError(
            t"Local trunk bookmark {ui.bookmark(trunk_branch)} is conflicted.",
            hint="Resolve it before landing.",
        )
    local_target = bookmark_state.local_target
    if local_target is not None and local_target != trunk_commit_id:
        inspect_command = f"jj log -r '{trunk_branch}|trunk()'"
        raise CliError(
            t"Local bookmark {ui.bookmark(trunk_branch)} points to a different "
            t"revision than {ui.revset('trunk()')}.",
            hint=(
                t"Inspect both with {ui.cmd(inspect_command)} and move "
                t"{ui.bookmark(trunk_branch)} back to {ui.revset('trunk()')} before "
                t"retrying."
            ),
        )

    remote_state = bookmark_state.remote_target(remote_name)
    if remote_state is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available.",
            hint="Fetch and retry.",
        )
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is "
            t"conflicted.",
            hint="Resolve it before landing.",
        )
    if remote_state.target is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available.",
            hint="Fetch and retry.",
        )
    if remote_state.target != trunk_commit_id:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} moved since "
            t"the selected path was resolved.",
            hint="Fetch, rebase if needed, and retry.",
        )


async def execute_land_plan(
    *,
    bookmark_cleanup_plans: tuple[ReviewBookmarkCleanupPlan, ...],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    plan: LandPlan,
    prepared_land: PreparedLand,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_subject: str,
) -> LandResult:
    """Execute a non-dry-run land plan and return the actions that were applied."""

    prepared_status = prepared_land.prepared_status
    prepared = prepared_status.prepared
    try:
        execution_state = prepare_land_execution_state(
            github_repository=github_repository,
            plan=plan,
            prepared_land=prepared_land,
            prepared_status=prepared_status,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
        )
    except CompletedLandResume as resume:
        return resume.result
    execution_plan = execution_state.execution_plan
    if execution_plan.blocked:
        return LandResult(
            actions=planned_land_actions(plan=execution_plan),
            applied=False,
            bypass_readiness=prepared_land.bypass_readiness,
            blocked=True,
            github_repository=github_repository.full_name,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
        )

    state = prepared.state_store.load()
    state_changes = dict(state.changes)
    land_intent = (
        execution_state.resume_intent.intent
        if execution_state.resume_intent is not None
        else build_land_intent(
            bypass_readiness=prepared_land.bypass_readiness,
            cleanup_bookmarks=prepared_land.cleanup_bookmarks,
            landed_revisions=execution_plan.landed_revisions,
            prepared_status=prepared_status,
            selected_pr_number=prepared_land.selected_pr_number,
            trunk_branch=trunk_branch,
        )
    )
    intent_path = (
        execution_state.resume_intent.path
        if execution_state.resume_intent is not None
        else write_new_intent(execution_state.state_dir, land_intent)
    )

    actions: list[LandAction] = []
    succeeded = False
    bookmark_cleanup_by_change_id = {
        cleanup_plan.change_id: cleanup_plan for cleanup_plan in bookmark_cleanup_plans
    }
    original_trunk_target = prepared.client.get_bookmark_state(trunk_branch).local_target
    try:
        if execution_plan.push_trunk:
            resubmit_revisions = execution_plan.resubmit_revisions
            if resubmit_revisions:
                console.output(
                    t"Refreshing {len(resubmit_revisions)} review "
                    t"{'branch' if len(resubmit_revisions) == 1 else 'branches'} "
                    t"to match the rebased local stack..."
                )
                for resubmit_revision in resubmit_revisions:
                    prepared.client.set_bookmark(
                        resubmit_revision.bookmark,
                        resubmit_revision.commit_id,
                        allow_backwards=True,
                    )
                prepared.client.push_bookmarks(
                    remote=remote_name,
                    bookmarks=tuple(revision.bookmark for revision in resubmit_revisions),
                )
                for resubmit_revision in resubmit_revisions:
                    actions.append(
                        LandAction(
                            kind="review branch",
                            body=t"refresh {ui.bookmark(resubmit_revision.bookmark)} to "
                            t"{resubmit_revision.subject} "
                            t"{ui.change_id(resubmit_revision.change_id)}",
                            status="applied",
                        )
                    )
                dismissed_action = await check_post_resubmit_approvals(
                    bypass_readiness=prepared_land.bypass_readiness,
                    github_client=github_client,
                    github_repository=github_repository,
                    resubmit_revisions=resubmit_revisions,
                    trunk_branch=trunk_branch,
                )
                if dismissed_action is not None:
                    actions.append(dismissed_action)
                    return LandResult(
                        actions=tuple(actions),
                        applied=True,
                        bypass_readiness=prepared_land.bypass_readiness,
                        blocked=True,
                        github_repository=github_repository.full_name,
                        remote_name=remote_name,
                        selected_revset=selected_revset,
                        trunk_branch=trunk_branch,
                        trunk_subject=trunk_subject,
                    )
            try:
                prepared.client.set_bookmark(
                    trunk_branch,
                    execution_plan.landed_revisions[-1].commit_id,
                )
                prepared.client.push_bookmarks(
                    remote=remote_name,
                    bookmarks=(trunk_branch,),
                )
            except BaseException:
                restore_local_trunk_bookmark(
                    client=prepared.client,
                    original_target=original_trunk_target,
                    trunk_branch=trunk_branch,
                )
                raise
            actions.append(
                LandAction(
                    kind="trunk",
                    body=t"push {ui.bookmark(trunk_branch)} to "
                    t"{execution_plan.landed_revisions[-1].subject} "
                    t"{ui.change_id(execution_plan.landed_revisions[-1].change_id)}",
                    status="applied",
                )
            )
        landed_head_change_id = (
            execution_plan.landed_revisions[-1].change_id
            if execution_plan.landed_revisions
            else None
        )
        for landed_index, landed_revision in enumerate(execution_plan.landed_revisions):
            console.output(
                t"Finalizing PR #{landed_revision.pull_request_number} for "
                t"{landed_revision.subject} "
                t"{ui.change_id(landed_revision.change_id)}..."
            )
            final_pull_request = await finalize_landed_pull_request(
                cached_change=state_changes.get(landed_revision.change_id),
                github_client=github_client,
                github_repository=github_repository,
                landed_revision=landed_revision,
                trunk_branch=trunk_branch,
            )
            actions.append(
                LandAction(
                    kind="pull request",
                    body=t"finalize PR #{landed_revision.pull_request_number} for "
                    t"{landed_revision.subject} "
                    t"{ui.change_id(landed_revision.change_id)}",
                    status="applied",
                )
            )
            landed_parent_change_id = (
                execution_plan.landed_revisions[landed_index - 1].change_id
                if landed_index > 0
                else None
            )
            state_changes[landed_revision.change_id] = updated_landed_change(
                bookmark=landed_revision.bookmark,
                bookmark_managed=landed_revision.bookmark_managed,
                cached_change=state_changes.get(landed_revision.change_id),
                commit_id=landed_revision.commit_id,
                parent_change_id=landed_parent_change_id,
                pull_request=final_pull_request,
                stack_head_change_id=landed_head_change_id,
            )
            prepared.state_store.save(state.model_copy(update={"changes": dict(state_changes)}))
            cleanup_plan = bookmark_cleanup_by_change_id.get(landed_revision.change_id)
            if cleanup_plan is not None:
                if cleanup_plan.can_forget:
                    prepared.client.forget_bookmarks((cleanup_plan.bookmark,))
                    actions.append(
                        LandAction(
                            kind="local bookmark",
                            body=t"forget {ui.bookmark(cleanup_plan.bookmark)} "
                            t"for {ui.change_id(landed_revision.change_id)}",
                            status="applied",
                        )
                    )
                else:
                    actions.append(cleanup_plan.action)
            land_intent = land_intent.model_copy(
                update={
                    "completed_change_ids": tuple(
                        dict.fromkeys(
                            (*land_intent.completed_change_ids, landed_revision.change_id)
                        )
                    )
                }
            )
            save_intent(intent_path, land_intent)
        succeeded = True
        return LandResult(
            actions=completed_land_actions(actions=tuple(actions), plan=execution_plan),
            applied=True,
            bypass_readiness=prepared_land.bypass_readiness,
            blocked=False,
            github_repository=github_repository.full_name,
            remote_name=remote_name,
            selected_revset=selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=trunk_subject,
        )
    finally:
        if succeeded:
            retire_superseded_intents(execution_state.stale_intents, land_intent)
            intent_path.unlink(missing_ok=True)


async def check_post_resubmit_approvals(
    *,
    bypass_readiness: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    resubmit_revisions: tuple[LandRevision, ...],
    trunk_branch: str,
) -> LandAction | None:
    """Return a blocking action if the resubmit push dismissed any approval."""

    if bypass_readiness or not resubmit_revisions:
        return None
    try:
        decisions = await github_client.get_review_decisions_by_pull_request_numbers(
            github_repository.owner,
            github_repository.repo,
            pull_numbers=tuple(
                revision.pull_request_number for revision in resubmit_revisions
            ),
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not re-check PR review decisions after refreshing review branches"
        ) from error
    for revision in resubmit_revisions:
        decision = decisions.get(revision.pull_request_number)
        if decision != "approved":
            return LandAction(
                kind="boundary",
                body=t"before pushing {ui.bookmark(trunk_branch)} because refreshing "
                t"{ui.bookmark(revision.bookmark)} dismissed the approval on "
                t"PR #{revision.pull_request_number}; request re-review and rerun "
                t"{ui.cmd('land')}",
                status="blocked",
            )
    return None


async def finalize_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    landed_revision: LandRevision,
    trunk_branch: str,
) -> GithubPullRequest:
    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=landed_revision.pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not load PR #{landed_revision.pull_request_number} during land"
        ) from error
    pull_request = normalize_pull_request_state(pull_request)
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
                base=trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to "
                t"{ui.bookmark(trunk_branch)}"
            ) from error
        pull_request = normalize_pull_request_state(pull_request)
    if pull_request.state == "open":
        try:
            await github_client.close_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
            pull_request = await github_client.get_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            raise CliError(t"Could not close PR #{pull_request.number} after landing") from error
        pull_request = normalize_pull_request_state(pull_request)
    if cached_change is not None:
        for comment_id, label in (
            (cached_change.navigation_comment_id, "stack navigation comment"),
            (cached_change.overview_comment_id, "stack overview comment"),
        ):
            if comment_id is None:
                continue
            try:
                await github_client.delete_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=comment_id,
                )
            except GithubClientError as error:
                if error.status_code != 404:
                    raise CliError(t"Could not delete {label} #{comment_id}") from error
    return pull_request


def updated_landed_change(
    *,
    bookmark: str,
    bookmark_managed: bool,
    cached_change: CachedChange | None,
    commit_id: str,
    parent_change_id: str | None,
    pull_request: GithubPullRequest,
    stack_head_change_id: str | None,
) -> CachedChange:
    pr_state = pull_request.state
    if pull_request.merged_at is not None:
        pr_state = "merged"
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            bookmark_ownership="managed" if bookmark_managed else "external",
            last_submitted_commit_id=commit_id,
            last_submitted_parent_change_id=parent_change_id,
            last_submitted_stack_head_change_id=stack_head_change_id,
            pr_number=pull_request.number,
            pr_state=pr_state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": "managed" if bookmark_managed else "external",
            "last_submitted_commit_id": commit_id,
            "last_submitted_parent_change_id": parent_change_id,
            "last_submitted_stack_head_change_id": stack_head_change_id,
            "pr_number": pull_request.number,
            "pr_review_decision": None,
            "pr_state": pr_state,
            "pr_url": pull_request.html_url,
            "navigation_comment_id": None,
            "overview_comment_id": None,
        }
    )
