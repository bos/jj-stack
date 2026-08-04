"""Create or update GitHub pull requests for the selected stack of changes.

This pushes or updates the review branches for that stack, then opens or refreshes one pull
request per change from bottom to top. Selected local changes must be free of unresolved
conflicts before `submit` pushes review branches or updates GitHub.

Pull request titles come from each change's subject line and bodies from the rest of the
description. When a description has no body, the repository's pull request template
(`.github/PULL_REQUEST_TEMPLATE.md`, `PULL_REQUEST_TEMPLATE.md`, or
`docs/PULL_REQUEST_TEMPLATE.md`) is used instead, and the subject line if there is no
template.

Use `--describe CHANGE=FILE` to read a prepared pull request body from a Markdown file,
or `--describe stack=FILE` to read prepared overview text for a multi-change stack.
Relative file paths are read from the current directory where `jj-stack` was invoked.

Use `--describe-with HELPER` to author pull request titles and bodies, and an overall
description of a stack. The helper can be interactive, in which case you enter these yourself,
or automated, such as invoking an LLM to generate these descriptions.

`jj-stack` invokes the helper as `helper --pr <change_id>` for each pull request and `helper
--stack <revset>` for the selected stack. The helper must output JSON with string `title` and
`body` fields.

Use `--edit` to review and edit the planned pull request titles, bodies, and draft states in your
editor before anything is pushed. Each `JJ: Draft:` field accepts `yes` or `no`, with `y` and `n`
as short forms. Saving the document continues the submit; a malformed document or a non-zero
editor exit aborts it before any change is made. The editor is the one jj's `ui.editor` setting
resolves to. `--edit` cannot be combined with `--describe-with`.

The `--label`, `--reviewers`, and `--team-reviewers` flags accept comma-separated values and may
be repeated. When passed, they override the corresponding configured defaults for this run.

Common examples:

- `jj-stack submit --dry-run` previews the current stack.

- `jj-stack submit` creates or refreshes its pull requests.

- `jj-stack submit <head-change-id>` selects another stack explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._github_stack_safety import dissolve_github_stack
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_stack.errors import CliError
from jj_stack.formatting import short_change_id
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import (
    require_github_repo,
    resolve_trunk_branch,
)
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient, ReviewRefUpdate
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalStack
from jj_stack.review.branches import (
    ResolvedReviewBranch,
    ensure_new_review_branches_unclaimed,
    ensure_unique_review_branches,
    review_branch_glob,
    review_branch_matches_change,
)
from jj_stack.review.selection import (
    parse_comma_separated_flag_values,
    resolve_selected_revset,
)
from jj_stack.state.operation_lock import acquire_operation_lock

from . import auto_close
from .auto_close import retarget_review_bases_before_branch_push
from .descriptions import edit_pull_requests_in_editor
from .github_stack import (
    GithubStackPlan,
    apply_github_stack_plan,
    plan_github_stack,
)
from .inputs import prepare_submit_inputs
from .models import (
    GeneratedDescription,
    PendingPullRequestSync,
    PreparedSubmitRevision,
    ResolvedSubmitOptions,
    SubmitDraftMode,
    SubmitMutationRun,
    SubmitOptions,
    SubmitResult,
    SubmittedRevision,
)
from .overview_comments import stack_overview_comment_bodies, sync_stack_overview_comments
from .pull_requests import (
    discover_pull_requests_by_branch,
    ensure_pull_request_syncs_are_safe,
    sync_pull_requests,
)
from .render import print_submit_result, render_selected_line
from .revisions import prepare_submit_revisions

HELP = "Send a jj stack to GitHub for review"


_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY


def submit(
    *,
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
    repository: Path | None,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
) -> int:
    """CLI entrypoint for `submit`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    options = _submit_options_from_cli(
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


def print_selected_line(selected_change_id: str, selected_subject: str) -> None:
    console.output(
        render_selected_line(
            selected_change_id=selected_change_id,
            selected_subject=selected_subject,
        )
    )


def _submit_options_from_cli(
    *,
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


def _resolve_submit_options(
    *,
    context: CommandContext,
    options: SubmitOptions,
) -> ResolvedSubmitOptions:
    config = context.config
    return ResolvedSubmitOptions(
        labels=config.labels if options.labels is None else options.labels,
        reviewers=config.reviewers if options.reviewers is None else options.reviewers,
        team_reviewers=(
            config.team_reviewers if options.team_reviewers is None else options.team_reviewers
        ),
    )


def _build_submit_result(
    *,
    client: JjClient,
    dry_run: bool,
    revisions: tuple[SubmittedRevision, ...],
    stack: LocalStack,
) -> SubmitResult:
    """Render one submit result from the shared stack context."""

    return SubmitResult(
        client=client,
        dry_run=dry_run,
        revisions=revisions,
        trunk=stack.trunk,
    )


def _pending_pull_request_syncs(
    *,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    drafts: dict[str, bool],
    generated_descriptions: dict[str, GeneratedDescription],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    trunk_branch: str,
) -> tuple[PendingPullRequestSync, ...]:
    """Build the desired pull-request sync plan for the submitted stack."""

    return tuple(
        PendingPullRequestSync(
            base_branch=prepared_revisions[index - 1].branch if index > 0 else trunk_branch,
            discovered_pull_request=discovered_pull_requests[prepared_revision.branch],
            draft=drafts[prepared_revision.revision.change_id],
            generated_description=generated_descriptions[prepared_revision.revision.change_id],
            prepared=prepared_revision,
        )
        for index, prepared_revision in enumerate(prepared_revisions)
    )


def _desired_draft_state(
    *,
    draft_mode: SubmitDraftMode,
    pull_request: GithubPullRequest | None,
) -> bool:
    """Resolve the command-wide draft flags for one pull request."""

    if pull_request is None:
        return draft_mode in ("draft", "draft_all")
    if draft_mode == "draft_all":
        return True
    if draft_mode == "open":
        return False
    return pull_request.is_draft


def _recover_interrupted_first_submissions(
    *,
    client: JjClient,
    remote: GitRemote,
    resolutions: tuple[ResolvedReviewBranch, ...],
    state_identities: Mapping[str, ReviewIdentity],
) -> tuple[ResolvedReviewBranch, ...]:
    """Reuse only one suffix candidate whose Git header proves the full change ID."""

    candidates_by_change: dict[str, dict[str, str]] = {}
    unresolved = tuple(
        resolution for resolution in resolutions if resolution.change_id not in state_identities
    )
    if not unresolved:
        return resolutions
    patterns = tuple(
        f"refs/heads/{review_branch_glob()}-{short_change_id(resolution.change_id)}"
        for resolution in unresolved
    )
    remote_candidates = client.list_remote_branches(remote=remote.name, patterns=patterns)
    for resolution in unresolved:
        candidates_by_change[resolution.change_id] = {
            branch: target
            for branch, target in remote_candidates.items()
            if review_branch_matches_change(branch, resolution.change_id)
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
                t"Could not prove that remote branch {ui.bookmark(branch)} belongs to "
                t"{ui.change_id(resolution.change_id)}.",
                hint="Inspect or remove that branch, then retry the submission.",
            )
        replacements[resolution.change_id] = branch, target

    recovered = tuple(
        (
            ResolvedReviewBranch(
                branch=replacements[resolution.change_id][0],
                change_id=resolution.change_id,
                recovered_target=replacements[resolution.change_id][1],
            )
            if resolution.change_id in replacements
            else resolution
        )
        for resolution in resolutions
    )
    ensure_unique_review_branches(recovered)
    return recovered


async def run_submit_async(
    *,
    context: CommandContext,
    on_prepared: Callable[[str, str], None] | None,
    options: SubmitOptions,
) -> SubmitResult:
    dry_run = options.dry_run
    state_store = context.state_store
    resolved_options = _resolve_submit_options(
        context=context,
        options=options,
    )
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

    if not stack.revisions:
        return _build_submit_result(
            client=client,
            dry_run=dry_run,
            revisions=(),
            stack=stack,
        )

    github_repository = require_github_repo(remote)
    branch_resolutions = _recover_interrupted_first_submissions(
        client=client,
        remote=remote,
        resolutions=prepared_inputs.branch_resolutions,
        state_identities=state.review_identities,
    )
    ensure_new_review_branches_unclaimed(
        branch_resolutions,
        state.review_identities,
        github_repository.repository_key,
    )
    visible_bookmarks = client.visible_review_bookmark_targets()
    collisions = tuple(
        resolution.branch
        for resolution in branch_resolutions
        if resolution.change_id not in state.review_identities
        and resolution.recovered_target is None
        and resolution.branch in visible_bookmarks
    )
    if collisions:
        raise CliError(
            t"Cannot claim visible bookmark {ui.join(ui.bookmark, collisions)} for a new review.",
            hint=t"Move work you need to keep outside the reserved namespace, or forget a stale "
            t"bookmark, then retry.",
        )
    remote_targets = client.list_remote_branches(
        remote=remote.name,
        patterns=tuple(f"refs/heads/{resolution.branch}" for resolution in branch_resolutions),
    )
    prepared_revisions = prepare_submit_revisions(
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
    tracked_pull_requests = {
        identity.head_ref: identity.pr_number
        for prepared in prepared_revisions
        if (identity := state.review_identities.get(prepared.revision.change_id)) is not None
    }
    submitted_revisions: tuple[SubmittedRevision, ...] = ()
    async with build_github_client(repository=github_repository) as github_client:
        generated_descriptions = prepared_inputs.generated_pull_request_descriptions
        with console.spinner(description="Inspecting GitHub"):
            try:
                (
                    github_repository_state,
                    discovered_pull_requests,
                    observed_stacks,
                ) = await asyncio.gather(
                    github_client.get_repository(),
                    discover_pull_requests_by_branch(
                        github_client=github_client,
                        branches=tuple(resolution.branch for resolution in branch_resolutions),
                        tracked_pull_requests=tracked_pull_requests,
                    ),
                    github_client.list_stacks(),
                )
            except GithubClientError as error:
                raise CliError(
                    f"Could not inspect GitHub repository {github_repository.full_name}"
                ) from error
            trunk_branch, trunk_targets = resolve_trunk_branch(
                client=client,
                github_repository_state=github_repository_state,
                remote=remote,
                trunk_commit_id=stack.trunk.commit_id,
            )
        drafts = {
            prepared.revision.change_id: _desired_draft_state(
                draft_mode=options.draft_mode,
                pull_request=discovered_pull_requests[prepared.branch],
            )
            for prepared in prepared_revisions
        }
        pending_syncs = _pending_pull_request_syncs(
            discovered_pull_requests=discovered_pull_requests,
            drafts=drafts,
            generated_descriptions=generated_descriptions,
            prepared_revisions=prepared_revisions,
            trunk_branch=trunk_branch,
        )
        ensure_pull_request_syncs_are_safe(
            options=options,
            pending_syncs=pending_syncs,
            repository_key=github_repository.repository_key,
            state=mutation_run.state,
        )
        if options.edit:
            generated_descriptions, drafts = edit_pull_requests_in_editor(
                descriptions=generated_descriptions,
                drafts=drafts,
                jj_client=client,
                revisions=stack.revisions,
            )
            pending_syncs = _pending_pull_request_syncs(
                discovered_pull_requests=discovered_pull_requests,
                drafts=drafts,
                generated_descriptions=generated_descriptions,
                prepared_revisions=prepared_revisions,
                trunk_branch=trunk_branch,
            )
        pushes_review_branches = any(
            revision.remote_action == "pushed" for revision in prepared_revisions
        )
        planned_branches = {revision.branch for revision in prepared_revisions}
        observed_base_refs = tuple(
            dict.fromkeys(
                pull_request.base.ref
                for pending in pending_syncs
                if (pull_request := pending.discovered_pull_request) is not None
                and pull_request.state == "open"
                and pull_request.base.ref not in planned_branches
                and pull_request.base.ref not in trunk_targets
                and pull_request.base.ref not in remote_targets
            )
        )
        observed_base_targets = client.list_remote_branches(
            remote=remote.name,
            patterns=tuple(f"refs/heads/{branch}" for branch in observed_base_refs),
        )
        retarget_syncs = (
            auto_close.predict_pull_requests_auto_closed_by_push(
                jj_client=client,
                pending_syncs=pending_syncs,
                prepared_revisions=prepared_revisions,
                remote_targets={
                    **trunk_targets,
                    **remote_targets,
                    **observed_base_targets,
                },
            )
            if pushes_review_branches
            else ()
        )
        desired_pull_numbers = tuple(
            pending.discovered_pull_request.number
            if pending.discovered_pull_request is not None
            else None
            for pending in pending_syncs
        )
        github_stack_plan = plan_github_stack(
            desired=desired_pull_numbers,
            observed_stacks=observed_stacks,
            pull_numbers_requiring_base_update={
                pull_request.number
                for pending in pending_syncs
                if (pull_request := pending.discovered_pull_request) is not None
                and (pull_request.base.ref != pending.base_branch or pending in retarget_syncs)
            },
        )
        if github_stack_plan.action == "replace" and not dry_run:
            assert (github_stack := github_stack_plan.affected_stack) is not None
            await dissolve_github_stack(github_client=github_client, stack=github_stack)
            github_stack_plan = GithubStackPlan("create" if len(pending_syncs) > 1 else "none")
        if not dry_run:
            # GitHub has no transaction spanning review branches, pull requests, and stack
            # membership. An external stack edit can race this mutation, and submit accepts that
            # narrow window rather than pretending another non-atomic observation closes it.
            if pushes_review_branches:
                await retarget_review_bases_before_branch_push(
                    github_client=github_client,
                    pending_syncs=retarget_syncs,
                    trunk_branch=trunk_branch,
                )
            with console.spinner(description="Pushing review branches"):
                client.mutate_remote_review_refs(
                    remote=remote.name,
                    updates=tuple(
                        ReviewRefUpdate(
                            branch=prepared.branch,
                            expected_target=prepared.expected_remote_target,
                            desired_target=prepared.revision.commit_id,
                        )
                        for prepared in prepared_revisions
                    ),
                )
        with console.progress(
            description="Syncing pull requests",
            total=len(prepared_revisions),
        ) as progress:
            submitted_revisions = await sync_pull_requests(
                github_client=github_client,
                on_progress=progress.advance,
                options=options,
                pending_syncs=pending_syncs,
                resolved_options=resolved_options,
                run=mutation_run,
            )

        if not dry_run:
            pull_numbers = tuple(
                pull_number
                for revision in submitted_revisions
                if (pull_number := revision.pull_request_number) is not None
            )
            if len(pull_numbers) != len(submitted_revisions):
                raise AssertionError(
                    "GitHub stack submit requires concrete pull request numbers."
                )
            await apply_github_stack_plan(
                github_client=github_client,
                plan=github_stack_plan,
                pull_numbers=pull_numbers,
            )
            await sync_stack_overview_comments(
                concurrency=_GITHUB_INSPECTION_CONCURRENCY,
                github_client=github_client,
                overview_bodies=stack_overview_comment_bodies(
                    generated_stack_description=prepared_inputs.generated_stack_description,
                    revisions=submitted_revisions,
                ),
            )
            await auto_close.verify_no_unexpected_pull_request_closures(
                discovered_pull_requests=discovered_pull_requests,
                github_client=github_client,
            )

    return _build_submit_result(
        client=client,
        dry_run=dry_run,
        revisions=submitted_revisions,
        stack=stack,
    )
