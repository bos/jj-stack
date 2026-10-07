"""Update pull requests after sync rebases their local changes."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.commands.github_run import GithubRun, ObservedTrunk
from jj_stack.commands.submit.descriptions import preserve_external_pr_text, read_pr_template
from jj_stack.commands.submit.inputs import (
    preflight_publication_stack,
    prepare_publication_inputs,
    sign_unsubmitted_commits,
)
from jj_stack.commands.submit.models import PreparedSubmitChange, PRMetadataAction
from jj_stack.commands.submit.publication import plan_pr_updates, publish_prepared
from jj_stack.errors import CliError, ConflictedStackError
from jj_stack.github.error_messages import read_or_stop
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.jj.client import change_ids_revset
from jj_stack.models.github import GithubStack
from jj_stack.stack.convergence_models import SelectedConvergencePlan
from jj_stack.stack.selected import select_stack_path


async def refresh_selected_prs(
    run: GithubRun,
    *,
    plan: SelectedConvergencePlan,
    github_stacks: tuple[GithubStack, ...],
    trunk: ObservedTrunk,
) -> None:
    if not plan.publish:
        return
    context = run.context
    if plan.remaining_changes and run.dry_run:
        _preview(plan)
        return
    if not plan.remaining_prs:
        if plan.remaining_changes:
            console.output("The remaining changes have no pull requests; they stay local.")
        return
    selected_ids = tuple(plan.remaining_prs)
    state = context.state_store.load()
    # Sign the rebased commits before reading them, so the push names the signed ones.
    sign_unsubmitted_commits(
        context.jj_client, revset=change_ids_revset(selected_ids), state=state
    )
    # The rebase changed local commits. PR identities and remote refs are still valid.
    path = select_stack_path(jj_client=context.jj_client, state=state, revset=selected_ids[-1])
    if tuple(change.change_id for change in path.stack.changes) != selected_ids:
        raise CliError("The remaining local stack changed during sync; inspect it and retry.")
    try:
        preflight_publication_stack(context.jj_client, path.stack)
        template = read_pr_template(context.jj_client.repo_root)
        if template is None:
            with console.spinner(
                description="Fetching pull request template from GitHub", report_changes=True
            ):
                template = await read_or_stop(
                    run.github.get_pr_template(),
                    message="Could not load the pull request template.",
                )
        inputs = prepare_publication_inputs(
            context=context,
            template=template,
            stack=path.stack,
            state=state,
            is_maximal_path=path.is_maximal,
        )
    except ConflictedStackError as error:
        raise ConflictedStackError(
            error.message,
            hint=t"Resolve the conflicts with {ui.cmd('jj')}, "
            t"then update the remaining pull requests with "
            t"{ui.cmd(f'jj-stack submit {short_change_id(selected_ids[-1])}')}.",
        ) from error
    prepared: list[PreparedSubmitChange] = []
    remote_targets: dict[str, CommitId] = {}
    drafts: dict[ChangeId, bool] = {}
    for change in path.stack.changes:
        change_id = change.change_id
        pr = plan.remaining_prs[change_id]
        prepared.append(
            PreparedSubmitChange(
                branch=pr.head.ref,
                expected_remote_target=pr.head.sha,
                change=change,
                pr=pr,
            )
        )
        drafts[change_id] = pr.is_draft
        # Sync planning checked that the PR head and remote branch name the same commit.
        remote_targets[pr.head.ref] = pr.head.sha
    changes = tuple(prepared)
    descriptions = preserve_external_pr_text(
        descriptions=inputs.generated_pr_descriptions,
        prs=plan.remaining_prs,
        submitted_descriptions=inputs.submitted_descriptions,
        template=inputs.pr_template,
    )
    plans = plan_pr_updates(
        bottom_base_branch=trunk.branch,
        drafts=drafts,
        generated_descriptions=descriptions,
        metadata=PRMetadataAction(
            context.config.labels,
            context.config.reviewers,
            context.config.team_reviewers,
        ),
        prepared_changes=changes,
        prior_reviewers={},
    )
    head = short_change_id(selected_ids[-1])
    cleanup_commands = tuple(
        f"jj-stack cleanup --pull-request {merged.candidate.pr_identity.pr_number}"
        for merged in plan.on_trunk
    )
    retry_hint: ui.Message = (
        t"The local stack has been updated. Finish updating the pull requests with "
        t"{ui.cmd(f'jj-stack submit {head}')}"
    )
    if cleanup_commands:
        retry_hint = (
            retry_hint,
            t", then clean up the merged pull requests with {ui.join(ui.cmd, cleanup_commands)}",
        )
    await publish_prepared(
        run,
        prepared_inputs=inputs,
        pr_plans=plans,
        remote_targets=remote_targets,
        retry_hint=(retry_hint, "."),
        observed_stacks=github_stacks,
        trunk=trunk,
        trunk_targets={trunk.branch: path.stack.trunk.commit_id},
    )


def _preview(plan: SelectedConvergencePlan) -> None:
    short = short_change_id(plan.remaining_changes[-1].change_id)
    if conflicted := tuple(item for item in plan.remaining_changes if item.conflict):
        # Sync would stop at these before updating any pull request.
        names = ui.join(lambda item: ui.change_id(item.change_id), conflicted)
        console.output(
            t"Sync would rebase the stack and stop at the conflicts in {names}. Resolve "
            t"them with {ui.cmd('jj')}, then update the remaining pull requests with "
            t"{ui.cmd(f'jj-stack submit {short}')}."
        )
        return
    console.output(
        t"Run {ui.cmd(f'jj-stack sync {short}')} to apply the "
        t"rebase and update the remaining pull requests."
    )
