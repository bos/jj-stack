"""Update pull requests after sync rebases their local changes."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands.submit.descriptions import preserve_external_pr_text
from jj_stack.commands.submit.inputs import prepare_publication_inputs
from jj_stack.commands.submit.models import PreparedSubmitChange, PRMetadataAction
from jj_stack.commands.submit.publication import plan_pr_updates, publish_prepared
from jj_stack.errors import CliError, ConflictedStackError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubTarget
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.models.github import GithubStack
from jj_stack.stack.convergence_models import ConvergenceActions
from jj_stack.stack.selected import select_stack_path


async def refresh_selected_prs(
    *,
    actions: ConvergenceActions,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
    github_stacks: tuple[GithubStack, ...],
    target: GithubTarget,
    trunk_branch: str,
) -> None:
    if not actions.on_trunk:
        return
    if actions.remaining_changes and dry_run:
        short = short_change_id(actions.remaining_changes[-1].change_id)
        console.output(
            t"Run {ui.cmd(f'jj-stack sync {short}')} to apply the "
            t"rebase and update the remaining pull requests."
        )
        return
    if not actions.remaining_prs:
        if actions.remaining_changes:
            console.output("The remaining changes have no pull requests; they stay local.")
        return
    selected_ids = tuple(actions.remaining_prs)
    state = context.state_store.load()
    # The rebase changed local commits. PR identities and remote refs are still valid.
    path = select_stack_path(jj_client=context.jj_client, state=state, revset=selected_ids[-1])
    if tuple(change.change_id for change in path.stack.changes) != selected_ids:
        raise CliError("The remaining local stack changed during sync; inspect it and retry.")
    try:
        inputs = prepare_publication_inputs(
            context=context,
            stack=path.stack,
            state=state,
            remote=target.remote,
            is_maximal_path=path.is_maximal,
        )
    except ConflictedStackError as error:
        raise ConflictedStackError(
            error.message,
            hint=t"The local rebase is complete. Resolve the conflicts with {ui.cmd('jj')}, "
            t"then update the remaining pull requests with "
            t"{ui.cmd(f'jj-stack submit {short_change_id(selected_ids[-1])}')}",
        ) from error
    prepared: list[PreparedSubmitChange] = []
    remote_targets: dict[str, CommitId] = {}
    drafts: dict[ChangeId, bool] = {}
    for change in path.stack.changes:
        change_id = change.change_id
        pr = actions.remaining_prs[change_id]
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
        prs=actions.remaining_prs,
        submitted_commits=inputs.submitted_commits,
        template=inputs.pr_template,
    )
    plans = plan_pr_updates(
        bottom_base_branch=trunk_branch,
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
    cleanup_commands = tuple(
        f"jj-stack cleanup --pull-request {merged.candidate.pr_identity.pr_number}"
        for merged in actions.on_trunk
    )
    await publish_prepared(
        context=context,
        github_client=github,
        prepared_inputs=inputs,
        pr_plans=plans,
        remote_targets=remote_targets,
        retry_hint=t"The local stack has been updated. Finish updating the pull requests with "
        t"{ui.cmd(f'jj-stack submit {short_change_id(selected_ids[-1])}')}, then clean up "
        t"the merged pull requests with {ui.join(ui.cmd, cleanup_commands)}.",
        observed_stacks=github_stacks,
        trunk_branch=trunk_branch,
        trunk_targets={trunk_branch: path.stack.trunk.commit_id},
        dry_run=False,
    )
