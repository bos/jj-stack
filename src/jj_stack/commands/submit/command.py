"""Create or update GitHub pull requests for the selected stack of changes.

This pushes or updates the review branches for that stack, then opens or refreshes one pull
request per change from bottom to top. Selected local changes must be free of unresolved
conflicts before `submit` changes local bookmarks, pushes review branches, or updates GitHub.

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

Use `--edit` to review and edit the planned pull request titles and bodies in your editor
before anything is pushed. Saving the document continues the submit; a malformed document or
a non-zero editor exit aborts it before any change is made. The editor is the one jj's
`ui.editor` setting resolves to. `--edit` cannot be combined with `--describe-with`.

The `--label`, `--reviewers`, and `--team-reviewers` flags accept comma-separated values and may
be repeated. When passed, they override the corresponding configured defaults for this run.

Common examples: `jj-stack submit --dry-run` previews the current stack;
`jj-stack submit` creates or refreshes its pull requests; and
`jj-stack submit <head-change-id>` selects another stack explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._github_stack_support import resolve_github_stack_support
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.resolution import (
    GithubRepoAddress,
    remote_bookmarks_pointing_at_commit,
    require_github_repo,
    resolve_trunk_branch,
)
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalStack
from jj_stack.review.observation import observe_reviews
from jj_stack.review.selection import (
    parse_comma_separated_flag_values,
    resolve_selected_revset,
)
from jj_stack.state.operation_lock import acquire_operation_lock

from . import auto_close
from .auto_close import retarget_review_bases_before_branch_push
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
from .native import NativeStackPlan, apply_native_stack_plan, plan_native_stack
from .pull_requests import (
    discover_pull_requests_by_bookmark,
    ensure_pull_request_syncs_are_safe,
    sync_pull_requests,
)
from .render import print_submit_result, render_selected_line
from .revisions import (
    prepare_submit_revisions,
    sync_local_bookmarks,
    sync_remote_bookmarks,
)
from .stack_comments import (
    navigation_comment_bodies,
    stack_overview_comment_bodies,
    sync_stack_comments,
)

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
    restart: bool,
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
        restart=restart,
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
    restart: bool,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
) -> SubmitOptions:
    selected_revset = resolve_selected_revset(
        command_label="submit",
        default_revset="@-",
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
        restart=restart,
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
    remote: GitRemote,
    revisions: tuple[SubmittedRevision, ...],
    stack: LocalStack,
    trunk_branch: str,
) -> SubmitResult:
    """Render one submit result from the shared stack context."""

    return SubmitResult(
        client=client,
        dry_run=dry_run,
        remote=remote,
        revisions=revisions,
        selected_change_id=stack.head.change_id,
        selected_revset=stack.selected_revset,
        selected_subject=stack.head.subject,
        trunk_change_id=stack.trunk.change_id,
        trunk_branch=trunk_branch,
        trunk=stack.trunk,
        trunk_subject=stack.trunk.subject,
    )


def _pending_pull_request_syncs(
    *,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    generated_descriptions: dict[str, GeneratedDescription],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    trunk_branch: str,
) -> tuple[PendingPullRequestSync, ...]:
    """Build the desired pull-request sync plan for the submitted stack."""

    stack_head_change_id = (
        prepared_revisions[-1].revision.change_id if prepared_revisions else None
    )
    return tuple(
        PendingPullRequestSync(
            base_branch=prepared_revisions[index - 1].bookmark if index > 0 else trunk_branch,
            discovered_pull_request=discovered_pull_requests[prepared_revision.bookmark],
            generated_description=generated_descriptions[prepared_revision.revision.change_id],
            parent_change_id=(
                prepared_revisions[index - 1].revision.change_id if index > 0 else None
            ),
            prepared=prepared_revision,
            stack_head_change_id=stack_head_change_id,
        )
        for index, prepared_revision in enumerate(prepared_revisions)
    )


def _validate_restart_recovery_candidates(
    *,
    head_owner: str,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    remote_targets: Mapping[str, str],
    restarted_change_ids: frozenset[str],
) -> None:
    """Accept only exact deterministic replacement PRs from an interrupted restart."""

    for pending in pending_syncs:
        prepared = pending.prepared
        change_id = prepared.revision.change_id
        pull_request = pending.discovered_pull_request
        if change_id not in restarted_change_ids:
            continue
        remote_target = remote_targets.get(prepared.bookmark)
        if remote_target not in {None, prepared.revision.commit_id}:
            raise CliError(
                t"Cannot restart {ui.change_id(change_id)} because replacement branch "
                t"{ui.bookmark(prepared.bookmark)} points to another commit.",
                hint="Inspect or remove the conflicting branch, then retry the restart.",
            )
        if pull_request is None:
            continue
        exact_head = (
            pull_request.head.ref == prepared.bookmark
            and pull_request.head.label == f"{head_owner}:{prepared.bookmark}"
            and pull_request.head.sha == prepared.revision.commit_id
        )
        if (
            exact_head
            and pull_request.base.ref == pending.base_branch
            and remote_target == prepared.revision.commit_id
        ):
            continue
        raise CliError(
            t"Cannot recover the restart of {ui.change_id(change_id)} from "
            t"PR #{pull_request.number} because its head commit, base, or remote branch changed.",
            hint=t"Inspect PR #{pull_request.number}, then adopt it with "
            t"{ui.cmd(f'relink {pull_request.number} {change_id}')} if it is the intended "
            t"replacement; otherwise resolve the conflicting PR or branch and retry.",
        )


async def _commit_restart_tracking(
    *,
    context: CommandContext,
    github_client: GithubClient,
    github_repository: GithubRepoAddress,
    mutation_run: SubmitMutationRun,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    remote: GitRemote,
    trunk_branch: str,
) -> None:
    """Freshly authorize every replacement, then save all pairs together."""

    if mutation_run.dry_run or not mutation_run.restarted_reviews:
        return
    replacements = mutation_run.restart_submissions
    identities: dict[str, ReviewIdentity] = {
        change_id: identity for change_id, (identity, _baseline) in replacements.items()
    }
    try:
        observation = await observe_reviews(
            change_ids=tuple(mutation_run.restarted_reviews),
            context=context,
            github_client=github_client,
            identity_overrides=identities,
            remote_name=remote.name,
            trunk_branch=trunk_branch,
        )
    except GithubClientError as error:
        raise CliError(
            "Could not verify replacement reviews after restarting the stack"
        ) from error

    repository_key = github_repository.repository_key
    repository_current = (
        observation.remote == remote
        and observation.configured_repository is not None
        and observation.configured_repository.repository_key == repository_key
        and observation.repository.repository_key == repository_key
        and observation.github_repository is not None
        and observation.github_repository.full_name.casefold()
        == github_repository.full_name.casefold()
    )
    pending_by_change = {
        pending.prepared.revision.change_id: pending for pending in pending_syncs
    }
    for change_id, (identity, baseline) in replacements.items():
        pending = pending_by_change[change_id]
        review = observation.reviews[change_id]
        pull_request = (
            review.pull_request.normalize_state() if review.pull_request is not None else None
        )
        exact = (
            repository_current
            and identity.repository_key == repository_key
            and change_id not in observation.duplicate_claim_change_ids
            and review.identity == identity
            and review.local_revision == pending.prepared.revision
            and pull_request is not None
            and identity.matches_pull_request(pull_request)
            and tuple(item.number for item in review.head_pull_requests)
            == (identity.pr_number,)
            and pull_request.state == "open"
            and pull_request.head.sha == pending.prepared.revision.commit_id
            and pull_request.base.ref == pending.base_branch
            and baseline.commit_id == pending.prepared.revision.commit_id
            and review.remote_review_target == pending.prepared.revision.commit_id
        )
        if not exact:
            raise CliError(
                t"Cannot save the restarted review for {ui.change_id(change_id)} because "
                t"its local change, remote branch, or GitHub pull request changed.",
                hint=t"Inspect PR #{identity.pr_number}, then adopt it with "
                t"{ui.cmd(f'relink {identity.pr_number} {change_id}')} if it is the intended "
                t"replacement; otherwise resolve the drift and rerun the same restart.",
            )
    mutation_run.commit_restart_submissions()


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
    with console.spinner(description="Preparing submit"):
        prepared_inputs = prepare_submit_inputs(
            context=context,
            options=options,
            resolved_options=resolved_options,
        )
    if on_prepared is not None:
        on_prepared(
            prepared_inputs.stack.head.change_id,
            prepared_inputs.stack.head.subject,
        )
    client = prepared_inputs.client
    remote = prepared_inputs.remote
    stack = prepared_inputs.stack
    bookmark_states = prepared_inputs.bookmark_states
    bookmark_resolutions = prepared_inputs.bookmark_resolutions
    state = prepared_inputs.state

    if not stack.revisions:
        trunk_branch = stack.trunk.subject
        remote_bookmarks = remote_bookmarks_pointing_at_commit(
            bookmark_states=client.list_bookmark_states(),
            remote_name=remote.name,
            commit_id=stack.trunk.commit_id,
        )
        if len(remote_bookmarks) == 1:
            trunk_branch = remote_bookmarks[0]
        return _build_submit_result(
            client=client,
            dry_run=dry_run,
            remote=remote,
            revisions=(),
            stack=stack,
            trunk_branch=trunk_branch,
        )

    github_repository = require_github_repo(remote)
    prepared_revisions = prepare_submit_revisions(
        bookmark_resolutions=bookmark_resolutions,
        bookmark_states=bookmark_states,
        client=client,
        remote=remote,
        stack=stack,
        state=state,
    )
    if not dry_run:
        state_store.require_writable()
    mutation_run = SubmitMutationRun(
        dry_run=dry_run,
        restarted_reviews={
            restarted.change.change_id: restarted
            for restarted in prepared_inputs.restarted_reviews
        },
        state=state,
        state_store=state_store,
    )
    submitted_revisions: tuple[SubmittedRevision, ...] = ()
    async with build_github_client(repository=github_repository) as github_client:
        with console.spinner(description="Inspecting GitHub"):
            try:
                (
                    github_repository_state,
                    discovered_pull_requests,
                    stack_support,
                ) = await asyncio.gather(
                    github_client.get_repository(),
                    discover_pull_requests_by_bookmark(
                        github_client=github_client,
                        bookmarks=tuple(
                            resolution.bookmark for resolution in bookmark_resolutions
                        ),
                    ),
                    resolve_github_stack_support(
                        github_client=github_client,
                        state_store=state_store,
                        persist=not dry_run,
                    ),
                )
            except GithubClientError as error:
                raise CliError(
                    f"Could not inspect GitHub repository {github_repository.full_name}"
                ) from error
            trunk_branch = resolve_trunk_branch(
                bookmark_states=bookmark_states,
                github_repository_state=github_repository_state,
                remote_name=remote.name,
                trunk_commit_id=stack.trunk.commit_id,
            )

        pending_syncs = _pending_pull_request_syncs(
            discovered_pull_requests=discovered_pull_requests,
            generated_descriptions=prepared_inputs.generated_pull_request_descriptions,
            prepared_revisions=prepared_revisions,
            trunk_branch=trunk_branch,
        )
        restart_bookmarks = tuple(
            pending.prepared.bookmark
            for pending in pending_syncs
            if pending.prepared.revision.change_id in prepared_inputs.restarted_change_ids
        )
        restart_remote_targets = client.list_remote_branches(
            remote=remote.name,
            patterns=tuple(f"refs/heads/{bookmark}" for bookmark in restart_bookmarks),
        )
        _validate_restart_recovery_candidates(
            head_owner=github_repository.owner,
            pending_syncs=pending_syncs,
            remote_targets=restart_remote_targets,
            restarted_change_ids=prepared_inputs.restarted_change_ids,
        )
        ensure_pull_request_syncs_are_safe(
            options=options,
            pending_syncs=pending_syncs,
            restarted_change_ids=prepared_inputs.restarted_change_ids,
            state=mutation_run.state,
        )
        pushes_review_branches = any(
            revision.remote_action == "pushed" for revision in prepared_revisions
        )
        retarget_syncs = (
            auto_close.predict_pull_requests_auto_closed_by_push(
                bookmark_states=bookmark_states,
                jj_client=client,
                pending_syncs=pending_syncs,
                prepared_revisions=prepared_revisions,
                remote_name=remote.name,
            )
            if pushes_review_branches and (stack_support.supported or not dry_run)
            else ()
        )
        native_plan = None
        if stack_support.supported:
            retiring_pull_numbers = tuple(
                restarted.identity.pr_number
                for restarted in prepared_inputs.restarted_reviews
                if restarted.identity.repository_key == github_repository.repository_key
            )
            desired_pull_numbers = tuple(
                pending.discovered_pull_request.number
                if pending.discovered_pull_request is not None
                else None
                for pending in pending_syncs
            )
            observed_stacks = stack_support.observed_stacks
            if observed_stacks is None and (retiring_pull_numbers or any(desired_pull_numbers)):
                try:
                    observed_stacks = await github_client.list_stacks()
                except GithubClientError as error:
                    raise CliError("Could not inspect native GitHub stack membership") from error
            native_plan = plan_native_stack(
                desired=desired_pull_numbers,
                observed_stacks=observed_stacks or (),
                pull_numbers_requiring_base_update={
                    pull_request.number
                    for pending in pending_syncs
                    if (pull_request := pending.discovered_pull_request) is not None
                    and (
                        pull_request.base.ref != pending.base_branch or pending in retarget_syncs
                    )
                },
                retiring_pull_numbers=retiring_pull_numbers,
            )
            if native_plan.action == "replace" and not dry_run:
                assert (native_stack := native_plan.affected_stack) is not None
                await GithubStackSelection(
                    github_client,
                    native_stack.pull_request_numbers,
                    state_store,
                ).dissolve_exact(observed=(native_stack,))
                native_plan = NativeStackPlan("create" if len(pending_syncs) > 1 else "none")
        sync_local_bookmarks(
            bookmark_states=bookmark_states,
            client=client,
            prepared_revisions=prepared_revisions,
            run=mutation_run,
            state=mutation_run.state,
        )
        if not dry_run and pushes_review_branches:
            await retarget_review_bases_before_branch_push(
                github_client=github_client,
                pending_syncs=retarget_syncs,
                trunk_branch=trunk_branch,
            )
            with console.spinner(description="Pushing review branches"):
                sync_remote_bookmarks(
                    client=client,
                    prepared_revisions=prepared_revisions,
                    remote=remote,
                    run=mutation_run,
                )
        else:
            sync_remote_bookmarks(
                client=client,
                prepared_revisions=prepared_revisions,
                remote=remote,
                run=mutation_run,
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
            if native_plan is not None:
                pull_numbers = tuple(
                    pull_number
                    for revision in submitted_revisions
                    if (pull_number := revision.pull_request_number) is not None
                )
                if len(pull_numbers) != len(submitted_revisions):
                    raise AssertionError("Native submit requires concrete pull request numbers.")
                await apply_native_stack_plan(
                    github_client=github_client,
                    plan=native_plan,
                    pull_numbers=pull_numbers,
                )
            await sync_stack_comments(
                concurrency=_GITHUB_INSPECTION_CONCURRENCY,
                github_client=github_client,
                navigation_bodies=(
                    {}
                    if native_plan is not None
                    else navigation_comment_bodies(
                        revisions=submitted_revisions,
                        trunk_branch=trunk_branch,
                    )
                ),
                overview_bodies=stack_overview_comment_bodies(
                    generated_stack_description=prepared_inputs.generated_stack_description,
                    revisions=submitted_revisions,
                ),
            )
            await auto_close.verify_no_unexpected_pull_request_closures(
                discovered_pull_requests=discovered_pull_requests,
                github_client=github_client,
            )
            await _commit_restart_tracking(
                context=context,
                github_client=github_client,
                github_repository=github_repository,
                mutation_run=mutation_run,
                pending_syncs=pending_syncs,
                remote=remote,
                trunk_branch=trunk_branch,
            )

    return _build_submit_result(
        client=client,
        dry_run=dry_run,
        remote=remote,
        revisions=submitted_revisions,
        stack=stack,
        trunk_branch=trunk_branch,
    )
