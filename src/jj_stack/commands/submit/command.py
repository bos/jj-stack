"""Create or update GitHub pull requests for the selected stack of changes.

This pushes or updates the PR branches for that stack, then opens or refreshes one pull
request per change from bottom to top. The selected changes must have no unresolved conflicts.

Pull request titles come from each change's subject line, and bodies come from the rest of the
description. When a description has no body, `jj-stack` uses the repo's pull request
template (`.github/PULL_REQUEST_TEMPLATE.md`, `PULL_REQUEST_TEMPLATE.md`, or
`docs/PULL_REQUEST_TEMPLATE.md`), or repeats the subject line if no template exists.

Use `--edit` to review and edit the planned pull request titles, bodies, and draft states in your
editor before anything is pushed. Each `JJ: Draft:` field accepts `yes` or `no`, with `y` and `n`
as short forms. Saving the document continues the command; a malformed document or a non-zero
editor exit aborts it before any change is made. `jj-stack` uses the editor selected by `jj`'s
`ui.editor` setting. `--edit` cannot be combined with `--describe-with`.

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
from dataclasses import replace
from pathlib import Path
from typing import cast

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_stack.errors import CliError, DriftError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import (
    require_github_repo,
    resolve_trunk_branch,
)
from jj_stack.github.stack_availability import github_stacks_unavailable_error
from jj_stack.identifiers import short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient, PRRefUpdate
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR, GithubRepo, GithubStack
from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import PRIdentity
from jj_stack.pr_branch_namespace import current_pr_branch_namespace, pr_branch_matches_change
from jj_stack.stack.github_stack_safety import dissolve_github_stack
from jj_stack.stack.pr_branches import (
    ResolvedPRBranch,
    ensure_new_pr_branches_unclaimed,
    ensure_unique_pr_branches,
)
from jj_stack.stack.selection import (
    parse_comma_separated_flag_values,
    resolve_selected_revset,
)
from jj_stack.state.operation_lock import acquire_operation_lock

from . import auto_close
from .auto_close import retarget_pr_bases_before_branch_push
from .changes import prepare_submit_changes
from .descriptions import edit_prs_in_editor
from .github_stack import (
    GithubStackPlan,
    apply_github_stack_plan,
    plan_github_stack,
)
from .inputs import prepare_submit_inputs
from .models import (
    GeneratedDescription,
    PreparedSubmitChange,
    PreparedSubmitInputs,
    PRMetadataAction,
    PRSyncPlan,
    SubmitDraftMode,
    SubmitMutationRun,
    SubmitOptions,
    SubmitResult,
    SubmittedChange,
)
from .overview_comments import stack_overview_comment_bodies, sync_stack_overview_comments
from .prs import (
    discover_prs_by_branch,
    ensure_pr_link_is_consistent,
    ensure_pr_syncs_are_safe,
    load_re_request_reviewers,
    sync_prs,
)
from .render import print_selected_line, print_submit_result

HELP = "Submit a `jj` stack for review"
DESCRIPTION_HELP = """
Use `--describe CHANGE=FILE` to read a prepared pull request body from a Markdown file, or
`--describe stack=FILE` to read prepared overview text for a multi-change stack. Relative file
paths are read from the current directory where `jj-stack` was invoked.

Use `--describe-with HELPER` to author pull request titles, bodies, and the stack overview. The
helper may be interactive or may generate the text noninteractively.

`jj-stack` invokes the helper as `helper --pr <change_id>` for each pull request and
`helper --stack <revset>` for the selected stack. The helper must output JSON with string `title`
and `body` fields.
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
    edit: bool,
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
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="submit",
    ):
        result = run_submit(
            context=context,
            # The selected line is only rendered when submit picked the
            # default head for the user.
            on_prepared=print_selected_line if revset is None else None,
            options=options,
        )
    print_submit_result(result)
    return 0


def run_submit(
    *,
    context: CommandContext,
    on_prepared: Callable[[str, str], None] | None,
    options: SubmitOptions,
) -> SubmitResult:
    """Run the full submit flow. The caller owns the operation lock."""

    return asyncio.run(
        run_submit_async(
            context=context,
            on_prepared=on_prepared,
            options=options,
        ),
    )


def _submit_options_from_cli(
    *,
    base: str | None,
    descriptions: Sequence[str] | None,
    describe_with: str | None,
    draft: bool,
    draft_all: bool,
    dry_run: bool,
    edit: bool,
    labels: Sequence[str] | None,
    open_: bool,
    re_request: bool,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
) -> SubmitOptions:
    selected_revset = resolve_selected_revset(
        command_label="submit",
        default_revset=None,
        require_explicit=False,
        revset=revset,
    )
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
        existing_only=False,
        labels=parse_comma_separated_flag_values(labels),
        re_request=re_request,
        reviewers=parse_comma_separated_flag_values(reviewers),
        revset=selected_revset,
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


def _build_submit_result(
    *,
    client: JjClient,
    dry_run: bool,
    changes: tuple[SubmittedChange, ...],
    stack: LocalStack,
) -> SubmitResult:
    """Render one submit result from the shared stack context."""

    return SubmitResult(
        client=client,
        dry_run=dry_run,
        changes=changes,
        trunk=stack.trunk,
    )


def _pr_sync_plans(
    *,
    bottom_base_branch: str,
    context: CommandContext,
    discovered_prs: dict[str, GithubPR | None],
    drafts: dict[str, bool],
    generated_descriptions: dict[str, GeneratedDescription],
    options: SubmitOptions,
    prepared_changes: tuple[PreparedSubmitChange, ...],
    prior_reviewers: Mapping[int, list[str]],
) -> tuple[PRSyncPlan, ...]:
    """Build one final desired-state plan after optional editing."""

    config = context.config
    labels = config.labels if options.labels is None else options.labels
    reviewers = config.reviewers if options.reviewers is None else options.reviewers
    team_reviewers = (
        config.team_reviewers if options.team_reviewers is None else options.team_reviewers
    )
    explicit_metadata = any(
        value is not None for value in (options.labels, options.reviewers, options.team_reviewers)
    )
    base_branches = (
        bottom_base_branch,
        *(change.branch for change in prepared_changes[:-1]),
    )
    plans: list[PRSyncPlan] = []
    for prepared, base_branch in zip(prepared_changes, base_branches, strict=True):
        pr = discovered_prs[prepared.branch]
        plan = PRSyncPlan(
            base_branch=base_branch,
            discovered_pr=pr,
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


def _github_inspection_results(
    *,
    discovered: dict[str, GithubPR | None] | BaseException,
    repo: GithubRepo | BaseException,
    repo_name: str,
    stacks: tuple[GithubStack, ...] | BaseException,
) -> tuple[GithubRepo, dict[str, GithubPR | None], tuple[GithubStack, ...]]:
    for kind, result in (("repo", repo), ("stacks", stacks), ("prs", discovered)):
        if not isinstance(result, BaseException):
            continue
        if kind == "stacks" and isinstance(result, GithubClientError):
            unavailable = github_stacks_unavailable_error(
                error=result,
                repo=repo_name,
            )
            if unavailable is not None:
                raise unavailable from None
        if isinstance(result, GithubClientError):
            raise CliError(f"Could not inspect GitHub repo {repo_name}") from result
        raise result
    return (
        cast(GithubRepo, repo),
        cast(dict[str, GithubPR | None], discovered),
        cast(tuple[GithubStack, ...], stacks),
    )


def _recover_interrupted_first_submissions(
    *,
    client: JjClient,
    remote: GitRemote,
    resolutions: tuple[ResolvedPRBranch, ...],
    state_identities: Mapping[str, PRIdentity],
) -> tuple[ResolvedPRBranch, ...]:
    """Reuse only one suffix candidate whose Git header records the full change ID."""

    candidates_by_change: dict[str, dict[str, str]] = {}
    unresolved = tuple(
        resolution for resolution in resolutions if resolution.change_id not in state_identities
    )
    if not unresolved:
        return resolutions
    namespace = current_pr_branch_namespace()
    patterns = tuple(
        f"refs/heads/{namespace.branch_glob}-{short_change_id(resolution.change_id)}"
        for resolution in unresolved
    )
    remote_candidates = client.list_remote_branches(remote=remote.name, patterns=patterns)
    for resolution in unresolved:
        candidates_by_change[resolution.change_id] = {
            branch: target
            for branch, target in remote_candidates.items()
            if pr_branch_matches_change(branch, resolution.change_id)
        }

    replacements: dict[str, tuple[str, str]] = {}
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
                hint="Inspect those branches and remove the unintended candidates, then retry.",
            )
        branch, target = next(iter(candidates.items()))
        if (
            client.read_remote_git_change_id(
                remote=remote.name,
                commit_id=target,
            )
            != resolution.change_id
        ):
            raise CliError(
                t"Remote branch {ui.bookmark(branch)} does not record the expected change ID "
                t"{ui.change_id(resolution.change_id)}.",
                hint="Inspect or remove that branch, then retry the submission.",
            )
        replacements[resolution.change_id] = branch, target

    recovered = tuple(
        (
            ResolvedPRBranch(
                branch=replacements[resolution.change_id][0],
                change_id=resolution.change_id,
                recovered_target=replacements[resolution.change_id][1],
            )
            if resolution.change_id in replacements
            else resolution
        )
        for resolution in resolutions
    )
    ensure_unique_pr_branches(recovered)
    return recovered


async def _apply_planned_submit(
    *,
    context: CommandContext,
    github_client: GithubClient,
    github_stack_plan: GithubStackPlan,
    prepared_inputs: PreparedSubmitInputs,
    pr_plans: tuple[PRSyncPlan, ...],
    pr_branch_ref_updates: tuple[PRRefUpdate, ...],
    retarget_plans: tuple[PRSyncPlan, ...],
    run: SubmitMutationRun,
    stacks_to_dissolve: tuple[GithubStack, ...],
    trunk_branch: str,
) -> tuple[SubmittedChange, ...]:
    if not run.dry_run:
        for github_stack in stacks_to_dissolve:
            await dissolve_github_stack(github_client=github_client, stack=github_stack)
        # GitHub has no transaction spanning PR branches, pull requests, and stack
        # membership. An external stack edit can race this mutation, and submit accepts that
        # narrow window rather than pretending another non-atomic observation closes it.
        if retarget_plans:
            await retarget_pr_bases_before_branch_push(
                github_client=github_client,
                plans=retarget_plans,
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
            run=run,
        )
    if not run.dry_run:
        pr_numbers = tuple(
            pr_number for change in submitted if (pr_number := change.pr_number) is not None
        )
        if len(pr_numbers) != len(submitted):
            raise AssertionError("GitHub stack submit requires concrete pull request numbers.")
        await apply_github_stack_plan(
            github_client=github_client,
            plan=github_stack_plan,
            pr_numbers=pr_numbers,
        )
        await sync_stack_overview_comments(
            concurrency=DEFAULT_BOUNDED_CONCURRENCY,
            github_client=github_client,
            overview_bodies=stack_overview_comment_bodies(
                generated_stack_description=prepared_inputs.generated_stack_description,
                changes=submitted,
            ),
        )
    return submitted


async def run_submit_async(
    *,
    context: CommandContext,
    on_prepared: Callable[[str, str], None] | None,
    options: SubmitOptions,
) -> SubmitResult:
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
    tracked_base = state.tracked_pr(explicit_base.change_id) if explicit_base else None
    assert explicit_base is None or tracked_base is not None, (
        "Prepared explicit base requires a tracked PR."
    )
    base_branch = tracked_base.pr_identity.head_ref if tracked_base is not None else None

    if not stack.changes:
        return _build_submit_result(
            client=client,
            dry_run=dry_run,
            changes=(),
            stack=stack,
        )

    github_repo = require_github_repo(remote)
    branch_resolutions = _recover_interrupted_first_submissions(
        client=client,
        remote=remote,
        resolutions=prepared_inputs.branch_resolutions,
        state_identities=state.pr_identities,
    )
    ensure_new_pr_branches_unclaimed(
        branch_resolutions,
        state.pr_identities,
        github_repo.repo_key,
    )
    visible_bookmarks = client.visible_pr_bookmark_targets()
    collisions = tuple(
        resolution.branch
        for resolution in branch_resolutions
        if resolution.change_id not in state.pr_identities
        and resolution.recovered_target is None
        and resolution.branch in visible_bookmarks
    )
    if collisions:
        raise CliError(
            t"Cannot claim visible bookmark {ui.join(ui.bookmark, collisions)} for a new PR.",
            hint=t"Move work you need to keep outside the reserved namespace, or forget a stale "
            t"bookmark, then retry.",
        )
    pr_branches = tuple(
        dict.fromkeys(
            (
                *(resolution.branch for resolution in branch_resolutions),
                *((base_branch,) if base_branch is not None else ()),
            )
        )
    )
    remote_targets = client.list_remote_branches(
        remote=remote.name,
        patterns=tuple(f"refs/heads/{branch}" for branch in pr_branches),
    )
    prepared_changes = prepare_submit_changes(
        branch_resolutions=branch_resolutions,
        remote_targets=remote_targets,
        remote=remote,
        stack=stack,
        state=state,
    )
    if not dry_run:
        state_store.require_writable()
    mutation_run = SubmitMutationRun(
        dry_run=dry_run,
        state=state,
        state_store=state_store,
    )
    tracked_prs = {
        identity.head_ref: identity.pr_number
        for prepared in prepared_changes
        if (identity := state.pr_identities.get(prepared.change.change_id)) is not None
    }
    if tracked_base is not None:
        tracked_prs[tracked_base.pr_identity.head_ref] = tracked_base.pr_identity.pr_number
    submitted_changes: tuple[SubmittedChange, ...] = ()
    async with build_github_client(repo=github_repo) as github_client:
        generated_descriptions = prepared_inputs.generated_pr_descriptions
        with console.spinner(description="Inspecting GitHub"):
            (
                github_repo_result,
                discovered_prs_result,
                observed_stacks_result,
            ) = await asyncio.gather(
                github_client.get_repo(),
                discover_prs_by_branch(
                    github_client=github_client,
                    branches=pr_branches,
                    tracked_prs=tracked_prs,
                ),
                github_client.list_stacks(),
                return_exceptions=True,
            )
            github_repo_state, discovered_prs, observed_stacks = _github_inspection_results(
                discovered=discovered_prs_result,
                repo=github_repo_result,
                repo_name=github_repo.full_name,
                stacks=observed_stacks_result,
            )
            trunk_branch, trunk_targets = resolve_trunk_branch(
                client=client,
                github_repo_state=github_repo_state,
                remote=remote,
                trunk_commit_id=stack.trunk.commit_id,
            )
        bottom_base_branch = trunk_branch
        if explicit_base is not None and tracked_base is not None and base_branch is not None:
            expected_base_commit = tracked_base.submitted_baseline.commit_id
            if remote_targets.get(base_branch) != expected_base_commit:
                remote_branch = f"{base_branch}@{remote.name}"
                child_retry = (
                    f"jj-stack submit --base {explicit_base.change_id} {stack.head.change_id}"
                )
                raise DriftError(
                    t"PR branch {ui.bookmark(remote_branch)} no longer "
                    t"points to the submitted commit for base "
                    t"{ui.change_id(explicit_base.change_id)}. jj-stack left it untouched and "
                    t"cannot repair it automatically.",
                    condition="remote_branch_moved",
                    hint=(
                        t"Externally restore {ui.bookmark(remote_branch)} to immutable submitted "
                        t"commit ID {ui.semantic_text(expected_base_commit, 'commit_id')}, then "
                        t"run {ui.cmd(child_retry)}."
                    ),
                )
            child_bottom = short_change_id(stack.changes[0].change_id)
            child_head = short_change_id(stack.head.change_id)
            child_rebase = f"jj rebase -r '{child_bottom}::{child_head}' -o 'trunk()'"
            ensure_pr_link_is_consistent(
                branch=base_branch,
                change_id=explicit_base.change_id,
                discovered_pr=discovered_prs[base_branch],
                expected_remote_target=expected_base_commit,
                repo_key=github_repo.repo_key,
                tracked_pr=tracked_base,
                merged_hint=(
                    t"Sync the parent PR first, rebase only the child stack with "
                    t"{ui.cmd(child_rebase)}, and then run "
                    t"{ui.cmd(f'jj-stack submit {child_head}')} without "
                    t"{ui.cmd('--base')}."
                ),
            )
            bottom_base_branch = base_branch
        drafts = {
            prepared.change.change_id: _desired_draft_state(
                draft_mode=options.draft_mode,
                pr=discovered_prs[prepared.branch],
            )
            for prepared in prepared_changes
        }
        ensure_pr_syncs_are_safe(
            discovered_prs=discovered_prs,
            existing_only=options.existing_only,
            prepared_changes=prepared_changes,
            repo_key=github_repo.repo_key,
            state=mutation_run.state,
        )
        if options.edit:
            generated_descriptions, drafts = edit_prs_in_editor(
                descriptions=generated_descriptions,
                drafts=drafts,
                jj_client=client,
                changes=stack.changes,
            )
        re_request_reviewers = (
            await load_re_request_reviewers(
                github_client=github_client,
                prs=tuple(
                    pr
                    for prepared in prepared_changes
                    if (pr := discovered_prs[prepared.branch]) is not None
                ),
            )
            if options.re_request and not dry_run
            else {}
        )
        pr_plans = _pr_sync_plans(
            bottom_base_branch=bottom_base_branch,
            context=context,
            discovered_prs=discovered_prs,
            drafts=drafts,
            generated_descriptions=generated_descriptions,
            options=options,
            prepared_changes=prepared_changes,
            prior_reviewers=re_request_reviewers,
        )
        pushes_pr_branches = any(change.remote_action == "pushed" for change in prepared_changes)
        planned_branches = {change.branch for change in prepared_changes}
        observed_base_refs = tuple(
            dict.fromkeys(
                pr.base.ref
                for plan in pr_plans
                if (pr := plan.discovered_pr) is not None
                and pr.state == "open"
                and pr.base.ref not in planned_branches
                and pr.base.ref not in trunk_targets
                and pr.base.ref not in remote_targets
            )
        )
        observed_base_targets = client.list_remote_branches(
            remote=remote.name,
            patterns=tuple(f"refs/heads/{branch}" for branch in observed_base_refs),
        )
        retarget_plans = (
            auto_close.predict_prs_auto_closed_by_push(
                jj_client=client,
                plans=pr_plans,
                prepared_changes=prepared_changes,
                remote_targets={**trunk_targets, **remote_targets, **observed_base_targets},
            )
            if pushes_pr_branches
            else ()
        )
        github_stack_plan = plan_github_stack(
            desired=tuple(
                plan.discovered_pr.number if plan.discovered_pr is not None else None
                for plan in pr_plans
            ),
            is_maximal_path=prepared_inputs.is_maximal_path,
            observed_stacks=observed_stacks,
            pr_numbers_requiring_base_update={
                pr.number
                for plan in pr_plans
                if (pr := plan.discovered_pr) is not None
                and (pr.base.ref != plan.base_branch or plan in retarget_plans)
            },
        )
        stacks_to_dissolve = (
            github_stack_plan.affected_stacks if github_stack_plan.action == "replace" else ()
        )
        if stacks_to_dissolve:
            github_stack_plan = GithubStackPlan("create" if len(pr_plans) > 1 else "none")
        pr_branch_ref_updates = tuple(
            PRRefUpdate(
                branch=prepared.branch,
                expected_target=prepared.expected_remote_target,
                desired_target=prepared.change.commit_id,
            )
            for prepared in prepared_changes
        )

        submitted_changes = await _apply_planned_submit(
            context=context,
            github_client=github_client,
            github_stack_plan=github_stack_plan,
            prepared_inputs=prepared_inputs,
            pr_plans=pr_plans,
            pr_branch_ref_updates=pr_branch_ref_updates,
            retarget_plans=retarget_plans,
            run=mutation_run,
            stacks_to_dissolve=stacks_to_dissolve,
            trunk_branch=trunk_branch,
        )
    return _build_submit_result(
        client=client,
        dry_run=dry_run,
        changes=submitted_changes,
        stack=stack,
    )
