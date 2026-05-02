"""Land the consecutive changes above `trunk()` that are ready to land now.

If your stack isn't based off `trunk()`, you'll need to `rebase` before landing.

To determine what to land, `land` walks up the stack until it reaches the top or a change that
it cannot land.

For a change to be landed, it must have no unresolved merge/rebase conflicts. Also, each pull
request must be open, not draft, approved, and have no outstanding changes requested. Use
`--bypass-readiness` to skip the draft / approval / changes-requested readiness checks.

Use `--dry-run` to inspect the landing plan without changing jj or GitHub state.

Use `--pull-request` to select the top of the stack to land by PR number or URL.

After a successful land, `jj-review` forgets the bookmarks it was managing for the changes that
landed, unless they've been moved or become conflicted. If you used your own bookmarks with
`submit --use-bookmarks`, they will not be cleaned up by default (override with `--config
jj-review.cleanup_user_bookmarks=true`). Use `--skip-cleanup` to keep even `jj-review`'s own
review bookmarks.

`land` does not touch changes above the first that could not be landed. In the usual direct-push
path, those remaining local changes keep the same base they already had, so no local rebase is
needed just because lower changes landed. Run `cleanup --rebase` only when some lower changes
were merged through different commit IDs and the local stack still contains those merged
ancestors; after that local rewrite, run `submit` to refresh the surviving review branches and
pull requests on GitHub.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClientError, build_github_client
from jj_review.github.resolution import (
    resolve_trunk_branch,
)
from jj_review.jj import JjCliArgs, JjClient
from jj_review.review.intents import retire_superseded_intents
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_review.review.status import (
    PreparedStatus,
    StatusResult,
    prepare_status,
    prepared_status_github_inspection_count,
    revision_has_merged_pull_request,
    stream_status,
)
from jj_review.state.intents import save_intent, write_new_intent

from .execute import (
    check_post_resubmit_approvals,
    ensure_trunk_branch_matches_selected_trunk,
    finalize_landed_pull_request,
    restore_local_trunk_bookmark,
    updated_landed_change,
)
from .models import (
    LandAction,
    LandResult,
    PreparedLand,
)
from .plan import (
    build_land_plan,
    completed_land_actions,
    make_divergence_classifier,
    plan_review_bookmark_cleanup_for_revisions,
    planned_land_actions,
)
from .render import print_land_result
from .resume import (
    CompletedLandResume,
    build_land_intent,
    prepare_land_execution_state,
)

HELP = "Land the ready changes at the bottom of a stack"


def land(
    *,
    bypass_readiness: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
    skip_cleanup: bool,
) -> int:
    """CLI entrypoint for `land`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    if pull_request is not None:
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="land",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(
            t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}"
        )
    else:
        pull_request_number = None
        resolved_revset = resolve_selected_revset(
            command_label="land",
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        )
    with console.spinner(description="Inspecting jj stack"):
        prepared_land = prepare_land(
            cleanup_bookmarks=not skip_cleanup,
            dry_run=dry_run,
            bypass_readiness=bypass_readiness,
            config=context.config,
            jj_client=context.jj_client,
            revset=resolved_revset,
            selected_pr_number=pull_request_number,
        )
    result = stream_land(prepared_land=prepared_land)
    print_land_result(result)
    return 1 if result.blocked else 0


def prepare_land(
    *,
    cleanup_bookmarks: bool,
    dry_run: bool,
    bypass_readiness: bool,
    config: RepoConfig,
    jj_client: JjClient,
    revset: str | None,
    selected_pr_number: int | None,
) -> PreparedLand:
    """Resolve local landing inputs before GitHub planning and execution."""

    prepared_status = prepare_status(
        config=config,
        fetch_remote_state=True,
        jj_client=jj_client,
        re_resolve_after_remote_refresh=True,
        revset=revset,
    )
    prepared = prepared_status.prepared
    if prepared.remote is None:
        message = prepared.remote_error or t"Could not determine which Git remote to use."
        raise CliError(message)
    if prepared_status.github_repository is None:
        message = prepared_status.github_repository_error or t"Could not resolve GitHub target."
        raise CliError(message)

    if not dry_run:
        prepared.state_store.require_writable()
    return PreparedLand(
        cleanup_bookmarks=cleanup_bookmarks,
        dry_run=dry_run,
        bypass_readiness=bypass_readiness,
        config=config,
        prepared_status=prepared_status,
        selected_pr_number=selected_pr_number,
    )


def stream_land(*, prepared_land: PreparedLand) -> LandResult:
    """Inspect GitHub state for the prepared path and optionally execute `land`."""

    prepared_status = prepared_land.prepared_status
    progress_total = prepared_status_github_inspection_count(
        prepared_status=prepared_status,
    )
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            inspect_stack_comments=False,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    return asyncio.run(
        stream_land_async(
            prepared_land=prepared_land,
            status_result=status_result,
        )
    )


async def stream_land_async(
    *,
    prepared_land: PreparedLand,
    status_result: StatusResult,
) -> LandResult:
    prepared_status = prepared_land.prepared_status
    prepared = prepared_status.prepared
    if status_result.github_error is not None:
        raise CliError(
            t"Could not inspect GitHub pull request state for {ui.cmd('land')}: "
            t"{status_result.github_error}"
        )
    if selected_stack_is_not_on_current_trunk(prepared_status=prepared_status):
        raise stack_not_on_trunk_error(
            prepared_status=prepared_status,
            status_result=status_result,
        )

    github_repository = prepared_status.github_repository
    remote = prepared.remote
    if github_repository is None or remote is None:
        raise AssertionError("Prepared land requires resolved GitHub and remote targets.")

    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            github_repository_state = await github_client.get_repository(
                github_repository.owner,
                github_repository.repo,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not load GitHub repository {github_repository.full_name}"
            ) from error
        with console.spinner(description="Loading bookmark state"):
            trunk_branch = resolve_trunk_branch(
                bookmark_states=prepared.client.list_bookmark_states(),
                github_repository_state=github_repository_state,
                remote_name=remote.name,
                trunk_commit_id=prepared.stack.trunk.commit_id,
            )
        ensure_trunk_branch_matches_selected_trunk(
            client=prepared.client,
            remote_name=remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        plan = build_land_plan(
            bypass_readiness=prepared_land.bypass_readiness,
            classify_divergence=make_divergence_classifier(prepared.client),
            prepared_status=prepared_status,
            status_result=status_result,
            trunk_branch=trunk_branch,
        )
        bookmark_cleanup_plans = plan_review_bookmark_cleanup_for_revisions(
            client=prepared.client,
            prefix=prepared_land.config.bookmark_prefix,
            cleanup_bookmarks=prepared_land.cleanup_bookmarks,
            cleanup_user_bookmarks=prepared_land.config.cleanup_user_bookmarks,
            landed_revisions=plan.landed_revisions,
        )
        if prepared_land.dry_run:
            return LandResult(
                actions=planned_land_actions(
                    plan=plan,
                    bookmark_cleanup_plans=bookmark_cleanup_plans,
                ),
                applied=False,
                bypass_readiness=prepared_land.bypass_readiness,
                blocked=plan.blocked,
                github_repository=github_repository.full_name,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=prepared.stack.trunk.subject,
            )

        try:
            execution_state = prepare_land_execution_state(
                github_repository=github_repository,
                plan=plan,
                prepared_land=prepared_land,
                prepared_status=prepared_status,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=prepared.stack.trunk.subject,
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
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=prepared.stack.trunk.subject,
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
                        remote=remote.name,
                        bookmarks=tuple(
                            revision.bookmark for revision in resubmit_revisions
                        ),
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
                            remote_name=remote.name,
                            selected_revset=status_result.selected_revset,
                            trunk_branch=trunk_branch,
                            trunk_subject=prepared.stack.trunk.subject,
                        )
                try:
                    prepared.client.set_bookmark(
                        trunk_branch,
                        execution_plan.landed_revisions[-1].commit_id,
                    )
                    prepared.client.push_bookmarks(
                        remote=remote.name,
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
                prepared.state_store.save(
                    state.model_copy(update={"changes": dict(state_changes)})
                )
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
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=prepared.stack.trunk.subject,
            )
        finally:
            if succeeded:
                retire_superseded_intents(execution_state.stale_intents, land_intent)
                intent_path.unlink(missing_ok=True)


def selected_stack_is_not_on_current_trunk(*, prepared_status: PreparedStatus) -> bool:
    prepared = prepared_status.prepared
    return (
        bool(prepared.stack.revisions)
        and prepared.stack.base_parent.commit_id != prepared.stack.trunk.commit_id
    )


def stack_not_on_trunk_error(
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
) -> CliError:
    message = t"Selected stack is not based on the current {ui.revset('trunk()')}."
    if any(revision_has_merged_pull_request(revision) for revision in status_result.revisions):
        return CliError(
            message,
            hint=(
                t"Some lower changes from this stack already landed. Run "
                t"{ui.cmd('cleanup --rebase')} {ui.revset(status_result.selected_revset)} "
                t"to rebase the remaining local changes before retrying."
            ),
        )

    bottom_change_id = prepared_status.prepared.status_revisions[0].revision.change_id
    rebase_command = f"jj rebase -s {short_change_id(bottom_change_id)} -d 'trunk()'"
    return CliError(
        message,
        hint=(
            t"No change in the selected stack has landed yet. Move the whole stack onto "
            t"{ui.revset('trunk()')} with {ui.cmd(rebase_command)} before retrying."
        ),
    )

