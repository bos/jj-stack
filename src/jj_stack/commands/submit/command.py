"""Create or update GitHub pull requests for the selected stack of changes.

Push the selected changes and create or update one PR per change, in local parent order.
Existing PRs stay linked to their changes. Resolve any conflicts before submitting.

The `--label`, `--reviewers`, and `--team-reviewers` flags accept comma-separated values and may
be repeated. When passed, they override the corresponding configured defaults for this run.

Common examples:

- `jj-stack submit --dry-run` previews the current stack.

- `jj-stack submit` creates or refreshes its pull requests.

- `jj-stack submit <head-change-id>` selects another stack explicitly.

- `jj-stack submit --base <parent-change-id> <child-head-change-id>` submits only the changes
  after an open parent pull request. Repeat `--base` whenever you refresh the child stack.

"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.config import parse_comma_separated_flag_values
from jj_stack.errors import CliError
from jj_stack.github.client import build_github_client
from jj_stack.github.error_messages import observe_github_repo, read_or_stop
from jj_stack.github.resolution import (
    require_github_repo,
    resolve_trunk_branch,
)
from jj_stack.identifiers import CommitId, short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.pr_branch_namespace import current_pr_branch_namespace, pr_branch_matches_change
from jj_stack.stack.change_state import ChangeObservation
from jj_stack.stack.pr_branches import (
    ResolvedPRBranch,
    ensure_new_pr_branches_unclaimed,
    ensure_unique_pr_branches,
    resolve_pr_branches,
)
from jj_stack.stack.pr_facts import observe_github_stacks
from jj_stack.stack.status import discover_pr_lookups
from jj_stack.state.operation_lock import operation_lock

from .changes import prepare_submit_changes, require_published_base
from .descriptions import edit_prs_in_editor, preserve_external_pr_text, resume_edit_hint
from .inputs import prepare_submit_inputs
from .models import (
    PRMetadataAction,
    SubmitDraftMode,
    SubmitOptions,
)
from .prs import (
    load_re_request_reviewers,
)
from .publication import plan_pr_updates, publish_prepared
from .render import print_selected_line, print_submit_rows

_BRANCH_LOOKUP_MESSAGE = "Could not inspect PR branches on GitHub."
_PR_LOOKUP_MESSAGE = "Could not inspect the selected pull requests."
HELP = "Create or update PRs for a jj stack"
DESCRIPTION_HELP = """
A pull request title comes from a change's subject line, and its body from the rest of the
description. When a description has no body, `jj-stack` uses the repo's pull request template
(`.github/PULL_REQUEST_TEMPLATE.md`, `PULL_REQUEST_TEMPLATE.md`, or
`docs/PULL_REQUEST_TEMPLATE.md`), or repeats the subject line if no template exists.

Later submits refresh the title and body from the change description, provided both still match
the defaults for the last submitted version. Editing either field on GitHub preserves both.

Use `--describe CHANGE=FILE` to read a PR body from a Markdown file, or `--describe stack=FILE`
to add an overview comment to the head PR of a stack with several changes. Relative paths are
resolved from the directory where you run `jj-stack`.

Use `--edit` to edit the planned titles, bodies, and draft states before anything is pushed.
Save and close the editor to continue. Invalid text or an editor error stops submission before
any branches or PRs change. If submission fails, the editor file is kept. Retry the same command
with `--resume-edit FILE` instead of `--edit`. The file must still name exactly the selected
changes.

The editor comes from `jj`'s `ui.editor`, then `$VISUAL`, then `$EDITOR`. Neither `--edit` nor
`--resume-edit` can be combined with `--describe-with`.

With `--describe-with HELPER`, jj-stack runs `helper --pr <change-id>` once per PR and
`helper --stack <revset>` once for a stack with several changes. Each call must print a JSON
object with string `title` and `body` fields.
"""


def submit(
    *,
    base: str | None,
    cli_args: JjCliArgs,
    debug: bool,
    descriptions: Sequence[str] | None,
    describe_with: str | None,
    draft: bool,
    draft_all: bool,
    dry_run: bool,
    edit: bool | Path,
    labels: Sequence[str] | None,
    open_: bool,
    re_request: bool,
    repo: Path | None,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
) -> int:
    """CLI entrypoint for `submit`."""

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    options = _submit_options_from_cli(
        base=base,
        descriptions=descriptions,
        describe_with=describe_with,
        draft=draft,
        draft_all=draft_all,
        dry_run=dry_run,
        edit=edit,
        labels=labels,
        open_=open_,
        re_request=re_request,
        reviewers=reviewers,
        revset=revset,
        team_reviewers=team_reviewers,
    )
    with operation_lock(
        context.state_store,
        command="submit",
        mutating=not dry_run,
    ):
        asyncio.run(
            run_submit_async(
                context=context,
                # The selected line is only rendered when submit picked the
                # default head for the user.
                on_prepared=print_selected_line if revset is None else None,
                options=options,
            )
        )
    return 0


def _submit_options_from_cli(
    *,
    base: str | None,
    descriptions: Sequence[str] | None,
    describe_with: str | None,
    draft: bool,
    draft_all: bool,
    dry_run: bool,
    edit: bool | Path,
    labels: Sequence[str] | None,
    open_: bool,
    re_request: bool,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
) -> SubmitOptions:
    return SubmitOptions(
        base_revset=base,
        descriptions=tuple(descriptions or ()),
        describe_with=describe_with,
        draft_mode=_submit_draft_mode(
            draft=draft,
            draft_all=draft_all,
            open_=open_,
        ),
        dry_run=dry_run,
        edit=edit,
        labels=parse_comma_separated_flag_values(labels),
        re_request=re_request,
        reviewers=parse_comma_separated_flag_values(reviewers),
        revset=revset,
        team_reviewers=parse_comma_separated_flag_values(team_reviewers),
    )


def _submit_draft_mode(
    *,
    draft: bool,
    draft_all: bool,
    open_: bool,
) -> SubmitDraftMode:
    if draft_all:
        return "draft_all"
    if draft:
        return "draft"
    if open_:
        return "open"
    return "default"


def _pr_metadata(*, context: CommandContext, options: SubmitOptions) -> PRMetadataAction:
    config = context.config
    return PRMetadataAction(
        labels=config.labels if options.labels is None else options.labels,
        reviewers=config.reviewers if options.reviewers is None else options.reviewers,
        team_reviewers=(
            config.team_reviewers if options.team_reviewers is None else options.team_reviewers
        ),
    )


def _desired_draft_state(
    *,
    draft_mode: SubmitDraftMode,
    pr: GithubPR | None,
) -> bool:
    """Resolve the command-wide draft flags for one pull request."""

    if pr is None:
        return draft_mode in ("draft", "draft_all")
    if draft_mode == "draft_all":
        return True
    if draft_mode == "open":
        return False
    return pr.is_draft


def _recover_interrupted_first_submissions(
    *,
    client: JjClient,
    remote: GitRemote,
    remote_targets: Mapping[str, CommitId],
    resolutions: tuple[ResolvedPRBranch, ...],
    tracked_prs: Mapping[str, TrackedPR],
) -> tuple[ResolvedPRBranch, ...]:
    """Reuse only one suffix candidate whose Git header records the full change ID."""

    candidates_by_change: dict[str, dict[str, str]] = {}
    unresolved = tuple(
        resolution for resolution in resolutions if resolution.change_id not in tracked_prs
    )
    if not unresolved:
        return resolutions
    for resolution in unresolved:
        candidates_by_change[resolution.change_id] = {
            branch: target
            for branch, target in remote_targets.items()
            if pr_branch_matches_change(branch, resolution.change_id)
        }

    replacements: dict[str, str] = {}
    for resolution in unresolved:
        candidates = candidates_by_change[resolution.change_id]
        if not candidates:
            continue
        if len(candidates) != 1:
            raise CliError(
                t"Could not recover the interrupted submission for "
                t"{ui.change_id(resolution.change_id)} because multiple remote branches "
                t"have its short change-ID suffix: "
                t"{ui.join(ui.bookmark, sorted(candidates))}.",
                hint="Inspect those branches, rename or remove any that belong to other work, "
                "then retry the same jj-stack submit command.",
            )
        branch, target = next(iter(candidates.items()))
        if (
            client.read_remote_git_commit(remote=remote.name, commit_id=target).change_id
            != resolution.change_id
        ):
            raise CliError(
                t"Remote branch {ui.bookmark(branch)} does not record the expected change ID "
                t"{ui.change_id(resolution.change_id)}.",
                hint="Inspect that branch and rename it if it belongs to other work. "
                "Then retry the same jj-stack submit command.",
            )
        replacements[resolution.change_id] = branch

    recovered = tuple(
        (
            ResolvedPRBranch(
                branch=replacements[resolution.change_id],
                change_id=resolution.change_id,
                recovered=True,
            )
            if resolution.change_id in replacements
            else resolution
        )
        for resolution in resolutions
    )
    ensure_unique_pr_branches(recovered)
    return recovered


def _submit_pr_branches(
    *,
    base_branch: str | None,
    resolutions: tuple[ResolvedPRBranch, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *(resolution.branch for resolution in resolutions),
                *((base_branch,) if base_branch is not None else ()),
            )
        )
    )


def _submit_remote_branch_queries(
    *,
    base_branch: str | None,
    resolutions: tuple[ResolvedPRBranch, ...],
    tracked_prs: Mapping[str, TrackedPR],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    exact_branches = tuple(
        dict.fromkeys(
            resolution.branch for resolution in resolutions if resolution.change_id in tracked_prs
        )
    )
    if base_branch is not None and base_branch not in exact_branches:
        exact_branches = (*exact_branches, base_branch)
    recovery_suffixes = tuple(
        dict.fromkeys(
            f"-{short_change_id(resolution.change_id)}"
            for resolution in resolutions
            if resolution.change_id not in tracked_prs
        )
    )
    return exact_branches, recovery_suffixes


async def run_submit_async(
    *,
    context: CommandContext,
    on_prepared: Callable[[str, str], None] | None,
    options: SubmitOptions,
) -> None:
    dry_run = options.dry_run
    state_store = context.state_store
    state = state_store.load()
    with console.spinner(description="Preparing submit"):
        prepared_inputs = prepare_submit_inputs(
            context=context,
            options=options,
            state=state,
        )
    if on_prepared is not None:
        on_prepared(
            prepared_inputs.stack.head.change_id,
            prepared_inputs.stack.head.subject,
        )
    client = prepared_inputs.client
    remote = prepared_inputs.remote
    stack = prepared_inputs.stack
    state = prepared_inputs.state
    explicit_base = stack.base_parent if options.base_revset is not None else None
    tracked_base = state.prs[explicit_base.change_id] if explicit_base is not None else None
    base_branch = tracked_base.pr_identity.head_ref if tracked_base is not None else None

    if not stack.changes:
        print_submit_rows(inputs=prepared_inputs, rows=(), heading="Submitted changes:")
        return

    github_repo = require_github_repo(remote)
    branch_resolutions = resolve_pr_branches(
        changes=stack.changes,
        tracked_prs=state.prs,
    )
    visible_bookmarks = client.visible_pr_bookmark_targets()
    initial_pr_branches = _submit_pr_branches(
        base_branch=base_branch,
        resolutions=branch_resolutions,
    )
    exact_remote_branches, recovery_suffixes = _submit_remote_branch_queries(
        base_branch=base_branch,
        resolutions=branch_resolutions,
        tracked_prs=state.prs,
    )

    def observations_by_branch(
        resolutions: tuple[ResolvedPRBranch, ...],
    ) -> dict[str, ChangeObservation]:
        changes: dict[str, LocalCommit] = {change.change_id: change for change in stack.changes}
        branches = {resolution.branch: resolution.change_id for resolution in resolutions}
        if explicit_base is not None and base_branch is not None:
            changes[explicit_base.change_id] = explicit_base
            branches[base_branch] = explicit_base.change_id
        return {
            branch: ChangeObservation(
                change_id=change_id,
                tracked=state.prs.get(change_id),
                branch=branch,
                remote_name=remote.name,
                local=(changes[change_id],),
                selected=changes[change_id],
            )
            for branch, change_id in branches.items()
        }

    generated_edit_path: Path | None = None
    async with build_github_client(repo=github_repo) as github_client:
        generated_descriptions = prepared_inputs.generated_pr_descriptions
        with console.spinner(description="Inspecting remotes"):
            exact_targets_task = asyncio.create_task(
                read_or_stop(
                    github_client.get_branch_targets(branches=exact_remote_branches),
                    message=_BRANCH_LOOKUP_MESSAGE,
                )
            )
            recovery_targets_task = asyncio.create_task(
                read_or_stop(
                    github_client.find_branch_targets_by_suffix(
                        branch_prefix=current_pr_branch_namespace().branch_prefix,
                        suffixes=recovery_suffixes,
                    ),
                    message=_BRANCH_LOOKUP_MESSAGE,
                )
            )
            repo_task = asyncio.create_task(observe_github_repo(github_client))
            lookups_task = asyncio.create_task(
                read_or_stop(
                    discover_pr_lookups(
                        github_client=github_client,
                        observations=observations_by_branch(branch_resolutions),
                    ),
                    message=_PR_LOOKUP_MESSAGE,
                )
            )
            stacks_task = asyncio.create_task(observe_github_stacks(github=github_client))
            await wait_for_read_tasks(
                exact_targets_task, recovery_targets_task, repo_task, lookups_task, stacks_task
            )
            remote_targets = {**exact_targets_task.result(), **recovery_targets_task.result()}
            branch_resolutions = _recover_interrupted_first_submissions(
                client=client,
                remote=remote,
                remote_targets=remote_targets,
                resolutions=branch_resolutions,
                tracked_prs=state.prs,
            )
            ensure_new_pr_branches_unclaimed(
                branch_resolutions,
                state.prs,
            )
            collisions = tuple(
                resolution.branch
                for resolution in branch_resolutions
                if resolution.change_id not in state.prs
                and not resolution.recovered
                and resolution.branch in visible_bookmarks
            )
            if collisions:
                raise CliError(
                    t"Local bookmark {ui.join(ui.bookmark, collisions)} already uses the name "
                    t"jj-stack would give a new PR branch.",
                    hint=t"Rename or forget that bookmark, then retry; jj-stack reserves the PR "
                    t"branch prefix for its own branches.",
                )
            pr_branches = _submit_pr_branches(
                base_branch=base_branch,
                resolutions=branch_resolutions,
            )
            lookups = lookups_task.result()
            if pr_branches != initial_pr_branches:
                lookups = await read_or_stop(
                    discover_pr_lookups(
                        github_client=github_client,
                        observations=observations_by_branch(branch_resolutions),
                    ),
                    message=_PR_LOOKUP_MESSAGE,
                )
            github_repo_state = repo_task.result()
            observed_stacks = stacks_task.result()
            trunk_branch, trunk_targets = resolve_trunk_branch(
                branches_at_trunk=client.remote_bookmarks_at_commit(
                    remote=remote.name,
                    commit_id=stack.trunk.commit_id,
                ),
                github_repo_state=github_repo_state,
                remote=remote,
                trunk_commit_id=stack.trunk.commit_id,
            )
        prepared_changes = prepare_submit_changes(
            branch_resolutions=branch_resolutions,
            lookups=lookups,
            remote_targets=remote_targets,
            stack=stack,
        )
        bottom_base_branch = trunk_branch
        if explicit_base is not None and tracked_base is not None and base_branch is not None:
            child_bottom = short_change_id(stack.changes[0].change_id)
            child_head = short_change_id(stack.head.change_id)
            child_rebase = f"jj rebase -s '{child_bottom}' -o 'trunk()'"
            require_published_base(
                base=explicit_base,
                lookup=lookups[base_branch],
                merged_hint=(
                    t"Sync the parent PR first, rebase only the child stack with "
                    t"{ui.cmd(child_rebase)}, and then run "
                    t"{ui.cmd(f'jj-stack submit {child_head}')} without "
                    t"{ui.cmd('--base')}."
                ),
                remote=remote,
                remote_target=remote_targets.get(base_branch),
                retry=(
                    f"jj-stack submit --base {short_change_id(explicit_base.change_id)} "
                    f"{child_head}"
                ),
                tracked_base=tracked_base,
            )
            bottom_base_branch = base_branch
        drafts: dict[str, bool] = {
            prepared.change.change_id: _desired_draft_state(
                draft_mode=options.draft_mode,
                pr=prepared.pr,
            )
            for prepared in prepared_changes
        }
        generated_descriptions = preserve_external_pr_text(
            descriptions=generated_descriptions,
            prs={prepared.change.change_id: prepared.pr for prepared in prepared_changes},
            repo_root=client.repo_root,
            submitted_commits=prepared_inputs.submitted_commits,
        )
        if options.edit:
            generated_descriptions, drafts, edit_path = edit_prs_in_editor(
                descriptions=generated_descriptions,
                drafts=drafts,
                jj_client=client,
                changes=stack.changes,
                document_path=options.edit if isinstance(options.edit, Path) else None,
            )
            if not isinstance(options.edit, Path):
                generated_edit_path = edit_path
                console.note(
                    t"Editor file: {ui.code(str(edit_path))} (kept if submission fails).",
                    soft_wrap=True,
                )
        re_request_reviewers = (
            await load_re_request_reviewers(
                github_client=github_client,
                prs=tuple(pr for prepared in prepared_changes if (pr := prepared.pr) is not None),
            )
            if options.re_request
            else {}
        )
        pr_plans = plan_pr_updates(
            bottom_base_branch=bottom_base_branch,
            drafts=drafts,
            generated_descriptions=generated_descriptions,
            metadata=_pr_metadata(context=context, options=options),
            explicit_metadata=bool(options.labels or options.reviewers or options.team_reviewers),
            prepared_changes=prepared_changes,
            prior_reviewers=re_request_reviewers,
        )
        await publish_prepared(
            context=context,
            github_client=github_client,
            prepared_inputs=prepared_inputs,
            pr_plans=pr_plans,
            remote_targets=remote_targets,
            retry_hint=(
                resume_edit_hint(generated_edit_path)
                if generated_edit_path is not None
                else t"Retry the same {ui.cmd('jj-stack submit')} command, "
                t"keeping its existing options."
            ),
            observed_stacks=observed_stacks,
            trunk_branch=trunk_branch,
            trunk_targets=trunk_targets,
            dry_run=dry_run,
        )
    if generated_edit_path is not None:
        try:
            generated_edit_path.unlink(missing_ok=True)
        except OSError:
            pass
