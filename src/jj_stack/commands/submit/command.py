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
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, GlobalOptions, bootstrap_context
from jj_stack.commands.github_run import GithubRun, ObservedTrunk
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.errors import CliError
from jj_stack.github.error_messages import observe_github_repo, read_or_stop
from jj_stack.github.resolution import GithubTarget, require_github_repo, select_submit_remote
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR, GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.pr_branch_namespace import current_pr_branch_namespace, pr_branch_matches_change
from jj_stack.stack.change_state import UNOBSERVED, ChangeObservation
from jj_stack.stack.pr_branches import (
    ResolvedPRBranch,
    ensure_new_pr_branches_unclaimed,
    ensure_unique_pr_branches,
    resolve_pr_branches,
)
from jj_stack.stack.pr_facts import observe_github_stacks
from jj_stack.stack.pr_rewrites import find_head_rewrites
from jj_stack.stack.status import discover_pr_lookups
from jj_stack.stack.trunk import observe_trunk_branch
from jj_stack.state.operation_lock import operation_lock

from .changes import prepare_submit_changes, require_published_base
from .descriptions import preserve_external_pr_text, read_pr_template
from .editor import edit_pr_document, parse_edited_pr_document, resume_edit_hint
from .inputs import prepare_publication_inputs, select_submit_inputs
from .models import (
    GeneratedDescription,
    PreparedSubmitChange,
    PRMetadataAction,
    PublicationInputs,
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
`docs/PULL_REQUEST_TEMPLATE.md`). If no local file exists, it asks GitHub for the default
template, including the owner's public `.github` repository fallback. Without a template,
it repeats the subject line.

Later submits refresh the title and body from the change description, provided both still match
the defaults for the last submitted version or another version in the change's `jj evolog`.
Editing either field on GitHub preserves both.

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


def submit(*, global_options: GlobalOptions, options: SubmitOptions) -> int:
    """CLI entrypoint for `submit`."""

    asyncio.run(run_submit_async(context=bootstrap_context(global_options), options=options))
    return 0


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
    tracked_prs: Mapping[ChangeId, TrackedPR],
) -> tuple[ResolvedPRBranch, ...]:
    """Reuse only one suffix candidate whose Git header records the full change ID."""

    unresolved = tuple(
        resolution for resolution in resolutions if resolution.change_id not in tracked_prs
    )
    if not unresolved:
        return resolutions
    replacements: dict[ChangeId, str] = {}
    for resolution in unresolved:
        candidates = {
            branch: target
            for branch, target in remote_targets.items()
            if pr_branch_matches_change(branch, resolution.change_id)
        }
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


def _submit_remote_branch_queries(
    *,
    base_branch: str | None,
    resolutions: tuple[ResolvedPRBranch, ...],
    tracked_prs: Mapping[ChangeId, TrackedPR],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    exact_branches = tuple(
        resolution.branch for resolution in resolutions if resolution.change_id in tracked_prs
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


@dataclass(frozen=True, slots=True)
class _SubmitObservation:
    """Local stack, tracking, and GitHub state observed before planning pull request updates."""

    bottom_base_branch: str
    drafts: dict[ChangeId, bool]
    generated_descriptions: dict[ChangeId, GeneratedDescription]
    observed_stacks: tuple[GithubStack, ...]
    prepared_changes: tuple[PreparedSubmitChange, ...]
    prepared_inputs: PublicationInputs
    remote_targets: dict[str, CommitId]
    trunk: ObservedTrunk
    trunk_targets: dict[str, CommitId]

    @property
    def changes(self) -> tuple[LocalCommit, ...]:
        return self.prepared_inputs.stack.changes


async def run_submit_async(
    *,
    context: CommandContext,
    options: SubmitOptions,
) -> None:
    # The selected line is only rendered when submit picked the default head for the user.
    print_selected = options.revset is None
    remote = select_submit_remote(context.jj_client.list_git_remotes())
    target = GithubTarget(remote=remote, repo=require_github_repo(remote))
    generated_edit_path: Path | None = None
    settle_pr_text: Callable[[_SubmitObservation], _SubmitObservation] | None = None
    async with context.open_github_client(repo=target.repo) as github_client:
        run = GithubRun(
            context=context, dry_run=options.dry_run, github=github_client, target=target
        )
        if options.edit or options.describe_with is not None:
            # An editor session or a describe helper can take as long as it likes, so neither
            # runs under the operation lock. The locked pass observes again and accepts their
            # text only if it still names the selected changes.
            observed = await _observe_submit(run, options=options, print_selected=print_selected)
            if observed is None:
                return
            print_selected = False
            if options.edit:
                document_path = edit_pr_document(
                    descriptions=observed.generated_descriptions,
                    drafts=observed.drafts,
                    jj_client=context.jj_client,
                    changes=observed.changes,
                    document_path=options.edit if isinstance(options.edit, Path) else None,
                )
                if not isinstance(options.edit, Path):
                    generated_edit_path = document_path
                    console.note(
                        t"Editor file: {ui.code(str(document_path))} (kept if submission fails).",
                        soft_wrap=True,
                    )
                settle_pr_text = partial(_apply_edited_document, document_path)
            else:
                settle_pr_text = partial(_apply_helper_text, observed.prepared_inputs)
                options = replace(options, describe_with=None)
        retry_hint: ui.Message = (
            resume_edit_hint(generated_edit_path)
            if generated_edit_path is not None
            else t"Retry the same {ui.cmd('jj-stack submit')} command, "
            t"keeping its existing options."
        )
        with ExitStack() as locked:
            try:
                locked.enter_context(
                    operation_lock(
                        context.state_store, command="submit", mutating=not run.dry_run
                    )
                )
                observed = await _observe_submit(
                    run, options=options, print_selected=print_selected
                )
            except CliError as error:
                if generated_edit_path is not None:
                    # The edited document outlives this failure; say how to reuse it.
                    error.hint = (
                        retry_hint if error.hint is None else (error.hint, " ", retry_hint)
                    )
                raise
            if observed is None:
                if settle_pr_text is None:
                    return
                raise CliError(
                    "The selected stack no longer has changes to submit.", hint=retry_hint
                )
            if settle_pr_text is not None:
                observed = settle_pr_text(observed)
            await _publish_observed(
                run,
                observed=observed,
                options=options,
                retry_hint=retry_hint,
            )
    if generated_edit_path is not None:
        try:
            generated_edit_path.unlink(missing_ok=True)
        except OSError:
            pass


def _apply_edited_document(
    document_path: Path, observed: _SubmitObservation
) -> _SubmitObservation:
    """Take titles, bodies, and draft choices from the edited document."""

    descriptions, drafts = parse_edited_pr_document(document_path, changes=observed.changes)
    return replace(observed, drafts=drafts, generated_descriptions=descriptions)


def _apply_helper_text(
    generated: PublicationInputs, observed: _SubmitObservation
) -> _SubmitObservation:
    """Take the describe helper's text, which must describe the selected changes."""

    described = set(generated.generated_pr_descriptions)
    if described != {change.change_id for change in observed.changes}:
        raise CliError(
            "The selected stack changed while the describe helper ran.",
            hint=t"Retry the same {ui.cmd('jj-stack submit')} command.",
        )
    return replace(
        observed,
        generated_descriptions=generated.generated_pr_descriptions,
        prepared_inputs=replace(
            observed.prepared_inputs,
            generated_pr_descriptions=generated.generated_pr_descriptions,
            generated_stack_description=generated.generated_stack_description,
        ),
    )


async def _observe_submit(
    run: GithubRun, *, options: SubmitOptions, print_selected: bool
) -> _SubmitObservation | None:
    """Observe the selected stack and its GitHub state; None when nothing is selected."""

    context, github_client, remote = run.context, run.github, run.target.remote
    state = context.state_store.load()
    with console.spinner(description="Preparing submit"):
        selection = select_submit_inputs(
            context=context,
            options=options,
            state=state,
        )
    client = context.jj_client
    stack = selection.stack
    explicit_base = selection.explicit_base
    base_branch = explicit_base.branch if explicit_base is not None else None
    if print_selected:
        print_selected_line(stack.head.change_id, stack.head.subject)

    if not stack.changes:
        print_submit_rows(client=client, trunk=stack.trunk, rows=(), heading="Submitted changes:")
        return None

    branch_resolutions = resolve_pr_branches(
        changes=stack.changes,
        tracked_prs=state.prs,
    )
    visible_bookmarks = client.visible_pr_bookmark_targets()
    initial_pr_branches = tuple(resolution.branch for resolution in branch_resolutions)
    exact_remote_branches, recovery_suffixes = _submit_remote_branch_queries(
        base_branch=base_branch,
        resolutions=branch_resolutions,
        tracked_prs=state.prs,
    )

    def observations_by_branch(
        resolutions: tuple[ResolvedPRBranch, ...],
    ) -> dict[str, ChangeObservation]:
        changes: dict[ChangeId, LocalCommit] = {
            change.change_id: change for change in stack.changes
        }
        branches = {resolution.branch: resolution.change_id for resolution in resolutions}
        if explicit_base is not None:
            changes[explicit_base.change.change_id] = explicit_base.change
            branches[explicit_base.branch] = explicit_base.change.change_id
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

    local_template = read_pr_template(client.repo_root)
    with console.spinner(
        description=(
            "Inspecting remotes and fetching pull request template from GitHub"
            if local_template is None
            else "Inspecting remotes"
        ),
        report_changes=local_template is None,
    ):
        exact_targets_task = asyncio.create_task(
            read_or_stop(
                github_client.get_publication_branches(
                    branches=exact_remote_branches,
                    include_pr_template=local_template is None and bool(exact_remote_branches),
                ),
                message=_BRANCH_LOOKUP_MESSAGE,
            )
        )
        recovery_targets_task = asyncio.create_task(
            read_or_stop(
                github_client.get_publication_branches_by_suffix(
                    branch_prefix=current_pr_branch_namespace().branch_prefix,
                    suffixes=recovery_suffixes,
                    include_pr_template=local_template is None and not exact_remote_branches,
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
        exact_targets, exact_template = exact_targets_task.result()
        recovery_targets, recovery_template = recovery_targets_task.result()
        github_template = exact_template if exact_remote_branches else recovery_template
        remote_targets = {**exact_targets, **recovery_targets}
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
        lookups = lookups_task.result()
        if tuple(resolution.branch for resolution in branch_resolutions) != initial_pr_branches:
            lookups = await read_or_stop(
                discover_pr_lookups(
                    github_client=github_client,
                    observations=observations_by_branch(branch_resolutions),
                ),
                message=_PR_LOOKUP_MESSAGE,
            )
        github_repo_state = repo_task.result()
        observed_stacks = stacks_task.result()
        trunk_branch, trunk_targets = observe_trunk_branch(
            jj_client=client,
            github_repo_state=github_repo_state,
            remote=remote,
            trunk_commit_id=stack.trunk.commit_id,
        )
    prepared_inputs = prepare_publication_inputs(
        context=context,
        template=github_template if local_template is None else local_template,
        stack=stack,
        state=state,
        is_maximal_path=selection.is_maximal_path,
        descriptions=options.descriptions,
        describe_with=options.describe_with,
    )
    rewrite_parents = await find_head_rewrites(
        client,
        github_client,
        remote=remote.name,
        trunk_branch=trunk_branch,
        chain=tuple(lookups[resolution.branch] for resolution in branch_resolutions),
    )
    lookups = {
        branch: replace(lookup, rewrite_parent=rewrite_parents.get(lookup.change_id, UNOBSERVED))
        for branch, lookup in lookups.items()
    }
    prepared_changes = prepare_submit_changes(
        branch_resolutions=branch_resolutions,
        lookups=lookups,
        remote_targets=remote_targets,
        stack=stack,
    )
    bottom_base_branch = trunk_branch
    if explicit_base is not None:
        require_published_base(
            base=explicit_base,
            lookup=lookups[explicit_base.branch],
            remote_target=remote_targets.get(explicit_base.branch),
            stack=stack,
        )
        bottom_base_branch = explicit_base.branch
    drafts: dict[ChangeId, bool] = {
        prepared.change.change_id: _desired_draft_state(
            draft_mode=options.draft_mode,
            pr=prepared.pr,
        )
        for prepared in prepared_changes
    }
    generated_descriptions = preserve_external_pr_text(
        descriptions=prepared_inputs.generated_pr_descriptions,
        prs={prepared.change.change_id: prepared.pr for prepared in prepared_changes},
        submitted_descriptions=prepared_inputs.submitted_descriptions,
        template=prepared_inputs.pr_template,
    )
    return _SubmitObservation(
        bottom_base_branch=bottom_base_branch,
        drafts=drafts,
        generated_descriptions=generated_descriptions,
        observed_stacks=observed_stacks,
        prepared_changes=prepared_changes,
        prepared_inputs=prepared_inputs,
        remote_targets=remote_targets,
        trunk=ObservedTrunk(github_repo=github_repo_state, branch=trunk_branch),
        trunk_targets=trunk_targets,
    )


async def _publish_observed(
    run: GithubRun,
    *,
    observed: _SubmitObservation,
    options: SubmitOptions,
    retry_hint: ui.Message,
) -> None:
    prepared_changes = observed.prepared_changes
    re_request_reviewers = (
        await load_re_request_reviewers(
            github_client=run.github,
            prs=tuple(pr for prepared in prepared_changes if (pr := prepared.pr) is not None),
        )
        if options.re_request
        else {}
    )
    pr_plans = plan_pr_updates(
        bottom_base_branch=observed.bottom_base_branch,
        drafts=observed.drafts,
        generated_descriptions=observed.generated_descriptions,
        metadata=_pr_metadata(context=run.context, options=options),
        explicit_metadata=bool(options.labels or options.reviewers or options.team_reviewers),
        prepared_changes=prepared_changes,
        prior_reviewers=re_request_reviewers,
    )
    await publish_prepared(
        run,
        prepared_inputs=observed.prepared_inputs,
        pr_plans=pr_plans,
        remote_targets=observed.remote_targets,
        retry_hint=retry_hint,
        observed_stacks=observed.observed_stacks,
        trunk=observed.trunk,
        trunk_targets=observed.trunk_targets,
    )
