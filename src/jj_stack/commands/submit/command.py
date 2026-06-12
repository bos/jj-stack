"""Create or update GitHub pull requests for the selected stack of changes.

This pushes or updates the GitHub branches for that stack, then opens or
refreshes one pull request per change from bottom to top. Selected local
changes must be free of unresolved conflicts before submit will mutate
bookmarks, remotes, or GitHub state.

Use `--describe-with HELPER` to author pull request titles and bodies, and an overall
description of a stack. The helper can be interactive, in which case you enter these yourself,
or automated, such as invoking an LLM to generate these descriptions.

 `jj-stack` invokes the helper as `helper --pr <change_id>` for each pull request and `helper
--stack <revset>` for the selected stack. The helper must output JSON with string `title` and
`body` fields.

The `--label`, `--reviewers`, `--team-reviewers`, and `--use-bookmarks` flags
accept comma-separated values and may be repeated. When passed, they override
the corresponding configured defaults for this run.

"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import (
    remote_bookmarks_pointing_at_commit,
    require_github_repo,
    resolve_trunk_branch,
)
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.stack import LocalStack
from jj_stack.review.bookmarks import BookmarkResolutionResult
from jj_stack.review.change_status import classify_saved_review_change
from jj_stack.review.selection import (
    parse_comma_separated_flag_values,
    resolve_selected_revset,
)
from jj_stack.state.journal import OperationJournal
from jj_stack.state.operation_lock import acquire_operation_lock

from .auto_close import (
    retarget_review_bases_before_branch_push,
    verify_no_unexpected_pull_request_closures,
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
from .pull_requests import discover_pull_requests_by_bookmark, sync_pull_requests
from .render import print_submit_result, render_selected_line
from .revisions import (
    prepare_submit_revisions,
    sync_local_bookmarks,
    sync_remote_bookmarks,
)
from .stack_comments import sync_stack_comments

HELP = "Send a jj stack to GitHub for review"


_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY


def submit(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    describe_with: str | None,
    draft: bool,
    draft_all: bool,
    dry_run: bool,
    labels: Sequence[str] | None,
    publish: bool,
    re_request: bool,
    repository: Path | None,
    restart: bool,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
    use_bookmarks: Sequence[str] | None,
) -> int:
    """CLI entrypoint for `submit`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    options = _submit_options_from_cli(
        describe_with=describe_with,
        draft=draft,
        draft_all=draft_all,
        dry_run=dry_run,
        labels=labels,
        publish=publish,
        re_request=re_request,
        restart=restart,
        reviewers=reviewers,
        revset=revset,
        team_reviewers=team_reviewers,
        use_bookmarks=use_bookmarks,
    )
    selection_emitter = _SubmitSelectionEmitter(enabled=revset is None)

    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="submit",
    ):
        result = asyncio.run(
            _run_submit_async(
                context=context,
                on_prepared=selection_emitter.emit_prepared,
                options=options,
            ),
        )
    selection_emitter.emit_fallback(result)
    print_submit_result(result)
    return 0


@dataclass(slots=True)
class _SubmitSelectionEmitter:
    """Render the default selected line exactly once."""

    enabled: bool
    emitted: bool = False

    def emit_prepared(
        self,
        selected_change_id: str,
        selected_subject: str,
    ) -> None:
        if self.enabled:
            console.output(
                render_selected_line(
                    selected_change_id=selected_change_id,
                    selected_subject=selected_subject,
                )
            )
        self.emitted = True

    def emit_fallback(self, result: SubmitResult) -> None:
        if self.enabled and not self.emitted:
            console.output(
                render_selected_line(
                    selected_change_id=result.selected_change_id,
                    selected_subject=result.selected_subject,
                )
            )


def _submit_options_from_cli(
    *,
    describe_with: str | None,
    draft: bool,
    draft_all: bool,
    dry_run: bool,
    labels: Sequence[str] | None,
    publish: bool,
    re_request: bool,
    restart: bool,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
    use_bookmarks: Sequence[str] | None,
) -> SubmitOptions:
    selected_revset = resolve_selected_revset(
        command_label="submit",
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )
    return SubmitOptions(
        describe_with=describe_with,
        draft_mode=_submit_draft_mode(
            draft=draft,
            draft_all=draft_all,
            publish=publish,
        ),
        dry_run=dry_run,
        labels=parse_comma_separated_flag_values(labels),
        re_request=re_request,
        restart=restart,
        reviewers=parse_comma_separated_flag_values(reviewers),
        revset=selected_revset,
        team_reviewers=parse_comma_separated_flag_values(team_reviewers),
        use_bookmarks=parse_comma_separated_flag_values(use_bookmarks),
    )


def _submit_draft_mode(
    *,
    draft: bool,
    draft_all: bool,
    publish: bool,
) -> SubmitDraftMode:
    if draft_all:
        return "draft_all"
    if draft:
        return "draft"
    if publish:
        return "publish"
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
            config.team_reviewers
            if options.team_reviewers is None
            else options.team_reviewers
        ),
        use_bookmarks=tuple(
            config.use_bookmarks
            if options.use_bookmarks is None
            else options.use_bookmarks
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


def _build_local_only_dry_run_result(
    *,
    client: JjClient,
    bookmark_result: BookmarkResolutionResult,
    bookmark_states: dict[str, BookmarkState],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote: GitRemote,
    remote_name: str,
    stack: LocalStack,
) -> SubmitResult | None:
    """Return a local-only dry-run result when no GitHub inspection is needed."""

    remote_bookmarks = remote_bookmarks_pointing_at_commit(
        bookmark_states=bookmark_states,
        remote_name=remote_name,
        commit_id=stack.trunk.commit_id,
    )
    if len(remote_bookmarks) != 1:
        return None

    revisions: list[SubmittedRevision] = []
    for prepared_revision in prepared_revisions:
        cached_change = bookmark_result.state.changes.get(prepared_revision.change_id)
        if classify_saved_review_change(
            cached_change,
            local="present",
        ).saved_review_identity:
            return None
        if prepared_revision.bookmark_source in {"discovered", "saved"}:
            return None
        bookmark_state = bookmark_states.get(
            prepared_revision.bookmark,
            BookmarkState(name=prepared_revision.bookmark),
        )
        if bookmark_state.remote_target(remote_name) is not None:
            return None
        revisions.append(
            SubmittedRevision(
                bookmark=prepared_revision.bookmark,
                bookmark_source=prepared_revision.bookmark_source,
                change_id=prepared_revision.change_id,
                commit_id=prepared_revision.revision.commit_id,
                local_action=prepared_revision.local_action,
                native_revision=prepared_revision.revision,
                pull_request_action="created",
                pull_request_is_draft=None,
                pull_request_number=None,
                pull_request_title=None,
                pull_request_url=None,
                remote_action=prepared_revision.remote_action,
                subject=prepared_revision.revision.subject,
            )
        )

    return _build_submit_result(
        client=client,
        dry_run=True,
        remote=remote,
        revisions=tuple(revisions),
        stack=stack,
        trunk_branch=remote_bookmarks[0],
    )


def _pending_pull_request_syncs(
    *,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    generated_descriptions: dict[str, GeneratedDescription],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    trunk_branch: str,
) -> tuple[PendingPullRequestSync, ...]:
    """Build the desired pull-request sync plan for the submitted stack."""

    stack_head_change_id = prepared_revisions[-1].change_id if prepared_revisions else None
    return tuple(
        PendingPullRequestSync(
            base_branch=prepared_revisions[index - 1].bookmark if index > 0 else trunk_branch,
            discovered_pull_request=discovered_pull_requests[prepared_revision.bookmark],
            generated_description=generated_descriptions[prepared_revision.change_id],
            parent_change_id=(
                prepared_revisions[index - 1].change_id if index > 0 else None
            ),
            prepared_revision=prepared_revision,
            stack_head_change_id=stack_head_change_id,
        )
        for index, prepared_revision in enumerate(prepared_revisions)
    )


def _reject_restart_pull_request_collisions(
    *,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    restarted_change_ids: frozenset[str],
) -> None:
    """Fail before push if a restart-selected branch already has an open PR."""

    if not restarted_change_ids:
        return
    collisions: list[tuple[PreparedSubmitRevision, GithubPullRequest]] = []
    for prepared_revision in prepared_revisions:
        if prepared_revision.change_id not in restarted_change_ids:
            continue
        pull_request = discovered_pull_requests.get(prepared_revision.bookmark)
        if pull_request is not None:
            collisions.append((prepared_revision, pull_request))
    if not collisions:
        return
    if len(collisions) == 1:
        prepared_revision, pull_request = collisions[0]
        raise CliError(
            t"Cannot restart {ui.change_id(prepared_revision.change_id)} with "
            t"{ui.bookmark(prepared_revision.bookmark)} because GitHub already reports "
            t"PR #{pull_request.number} for that branch.",
            hint=t"Run {ui.cmd('jj-stack view --fetch')} and retry with current state.",
        )
    details = ui.join(
        lambda item: t"{ui.change_id(item[0].change_id)} -> PR #{item[1].number}",
        collisions,
    )
    raise CliError(
        t"Cannot restart with fresh PRs because GitHub already reports PRs for "
        t"the selected replacement branches: {details}.",
        hint=t"Run {ui.cmd('jj-stack view --fetch')} and retry with current state.",
    )


async def _run_submit_async(
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
            on_prepared=on_prepared,
            options=options,
            resolved_options=resolved_options,
        )
    client = prepared_inputs.client
    remote = prepared_inputs.remote
    stack = prepared_inputs.stack
    bookmark_states = prepared_inputs.bookmark_states
    bookmark_result = prepared_inputs.bookmark_result
    state = prepared_inputs.state

    if not stack.revisions:
        if bookmark_result.changed and not dry_run:
            state_store.save(bookmark_result.state)
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
        bookmark_result=bookmark_result,
        bookmark_states=bookmark_states,
        client=client,
        remote=remote,
        stack=stack,
    )
    state_changes = dict(bookmark_result.state.changes)
    journal = OperationJournal.disabled()
    if not dry_run:
        state_dir = state_store.require_writable()
        journal = OperationJournal.begin(
            state_dir,
            operation="submit",
            options={
                "remote_name": remote.name,
                "github_host": github_repository.host,
                "github_owner": github_repository.owner,
                "github_repo": github_repository.repo,
            },
            resolved_scope={
                "bookmarks": {
                    revision.change_id: resolution.bookmark
                    for revision, resolution in zip(
                        stack.revisions,
                        bookmark_result.resolutions,
                        strict=True,
                    )
                },
                "ordered_change_ids": tuple(
                    revision.change_id for revision in stack.revisions
                ),
                "ordered_commit_ids": tuple(
                    revision.commit_id for revision in stack.revisions
                ),
                "selected_revset": stack.selected_revset,
            },
        )
    mutation_run = SubmitMutationRun(
        dry_run=dry_run,
        journal=journal,
        state=bookmark_result.state,
        state_changes=state_changes,
        state_store=state_store,
    )
    if dry_run:
        if not prepared_inputs.restarted_change_ids:
            local_only_dry_run = _build_local_only_dry_run_result(
                client=client,
                bookmark_result=bookmark_result,
                bookmark_states=bookmark_states,
                prepared_revisions=prepared_revisions,
                remote=remote,
                remote_name=remote.name,
                stack=stack,
            )
            if local_only_dry_run is not None:
                return local_only_dry_run

    succeeded = False
    submitted_revisions: tuple[SubmittedRevision, ...] = ()
    try:
        async with build_github_client(repository=github_repository) as github_client:
            with console.spinner(description="Inspecting GitHub"):
                try:
                    github_repository_state, discovered_pull_requests = await asyncio.gather(
                        github_client.get_repository(
                        ),
                        discover_pull_requests_by_bookmark(
                            github_client=github_client,
                            bookmarks=tuple(
                                resolution.bookmark
                                for resolution in bookmark_result.resolutions
                            ),
                        ),
                    )
                except GithubClientError as error:
                    raise CliError(
                        f"Could not load GitHub repository {github_repository.full_name}"
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
            _reject_restart_pull_request_collisions(
                discovered_pull_requests=discovered_pull_requests,
                prepared_revisions=prepared_revisions,
                restarted_change_ids=prepared_inputs.restarted_change_ids,
            )
            sync_local_bookmarks(
                bookmark_result=bookmark_result,
                bookmark_states=bookmark_states,
                client=client,
                prepared_revisions=prepared_revisions,
                run=mutation_run,
            )
            if not dry_run and any(
                revision.remote_action == "pushed" for revision in prepared_revisions
            ):
                await retarget_review_bases_before_branch_push(
                    bookmark_states=bookmark_states,
                    github_client=github_client,
                    jj_client=client,
                    pending_syncs=pending_syncs,
                    prepared_revisions=prepared_revisions,
                    remote_name=remote.name,
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
                await sync_stack_comments(
                    concurrency=_GITHUB_INSPECTION_CONCURRENCY,
                    generated_stack_description=prepared_inputs.generated_stack_description,
                    github_client=github_client,
                    revisions=submitted_revisions,
                    run=mutation_run,
                    trunk_branch=trunk_branch,
                )
                await verify_no_unexpected_pull_request_closures(
                    discovered_pull_requests=discovered_pull_requests,
                    github_client=github_client,
                )

        if not dry_run:
            next_state = bookmark_result.state.model_copy(update={"changes": state_changes})
            if bookmark_result.changed or next_state != state:
                state_store.save(next_state)

        succeeded = True
        return _build_submit_result(
            client=client,
            dry_run=dry_run,
            remote=remote,
            revisions=submitted_revisions,
            stack=stack,
            trunk_branch=trunk_branch,
        )
    finally:
        if succeeded:
            completed_change_ids = tuple(revision.change_id for revision in stack.revisions)
            journal.append(
                "completed",
                {"ordered_change_ids": completed_change_ids},
            )
