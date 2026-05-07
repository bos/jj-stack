"""Create or update GitHub pull requests for the selected stack of changes.

This pushes or updates the GitHub branches for that stack, then opens or
refreshes one pull request per change from bottom to top. Selected local
changes must be free of unresolved conflicts before submit will mutate
bookmarks, remotes, or GitHub state.

Use `--describe-with HELPER` to delegate pull request titles and bodies plus
stack-comment prose. `jj-review` invokes the helper as
`helper --pr <change_id>` for each pull request and `helper --stack <revset>`
for the selected stack; the helper must print JSON with string `title` and
`body` fields.

The `--label`, `--reviewers`, `--team-reviewers`, and `--use-bookmarks` flags
accept comma-separated values and may be repeated. When passed, they override
the corresponding configured defaults for this run.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    remote_bookmarks_pointing_at_commit,
    require_github_repo,
    resolve_trunk_branch,
    select_submit_remote,
)
from jj_review.jj import JjCliArgs, JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import (
    GithubPullRequest,
    GithubPullRequestReview,
)
from jj_review.models.intent import LoadedIntent, SubmitIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.bookmarks import (
    BookmarkResolutionResult,
    BookmarkResolver,
    BookmarkSource,
    bookmark_ownership_for_source,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
    match_bookmarks_for_revisions,
)
from jj_review.review.intents import describe_intent, retire_superseded_intents
from jj_review.review.selection import (
    parse_comma_separated_flag_values,
    resolve_selected_revset,
)
from jj_review.review.submit_recovery import (
    SubmitRecoveryIdentity,
    SubmitStatusDecision,
    submit_status_decision,
)
from jj_review.state.intents import (
    check_same_kind_intent,
    scan_intents,
    write_new_intent,
)
from jj_review.state.store import ReviewStateStore
from jj_review.system import pid_is_alive

from .descriptions import resolve_generated_descriptions
from .models import (
    GeneratedDescription,
    InterruptedRemoteBookmarkRepairer,
    LocalBookmarkAction,
    PendingPullRequestSync,
    PreparedSubmitInputs,
    PreparedSubmitRevision,
    PrivateCommitFinder,
    PullRequestAction,
    PullRequestSyncResult,
    PushOperation,
    RemoteBookmarkAction,
    RemoteBookmarkSyncer,
    SubmitDraftMode,
    SubmitIntentState,
    SubmitResult,
    SubmittedPullRequestSync,
    SubmittedRevision,
)
from .render import print_submit_result, render_selected_line
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
    selected_revset = resolve_selected_revset(
        command_label="submit",
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )
    label_list = parse_comma_separated_flag_values(labels)
    reviewer_list = parse_comma_separated_flag_values(reviewers)
    team_reviewer_list = parse_comma_separated_flag_values(team_reviewers)
    use_bookmark_list = parse_comma_separated_flag_values(use_bookmarks)
    emitted_prepared = False

    def emit_prepared(
        selected_change_id: str,
        selected_subject: str,
    ) -> None:
        nonlocal emitted_prepared
        if revset is None:
            console.output(
                render_selected_line(
                    selected_change_id=selected_change_id,
                    selected_subject=selected_subject,
                )
            )
        emitted_prepared = True

    state_store = ReviewStateStore.for_repo(context.repo_root)
    result = asyncio.run(
        _run_submit_async(
            config=context.config,
            describe_with=describe_with,
            draft_mode=(
                "draft_all"
                if draft_all
                else "draft" if draft else "publish" if publish else "default"
            ),
            dry_run=dry_run,
            jj_client=context.jj_client,
            labels=label_list,
            on_prepared=emit_prepared,
            re_request=re_request,
            revset=selected_revset,
            reviewers=reviewer_list,
            state_store=state_store,
            team_reviewers=team_reviewer_list,
            use_bookmarks=use_bookmark_list,
        )
    )
    if not emitted_prepared:
        if revset is None:
            console.output(
                render_selected_line(
                    selected_change_id=result.selected_change_id,
                    selected_subject=result.selected_subject,
                )
            )
    print_submit_result(result)
    return 0


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


def _prepare_submit_inputs(
    *,
    config: RepoConfig,
    describe_with: str | None,
    dry_run: bool,
    jj_client: JjClient,
    on_prepared: Callable[[str, str], None] | None,
    revset: str | None,
    state_store: ReviewStateStore,
    use_bookmarks: tuple[str, ...],
) -> PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = jj_client
    remote = select_submit_remote(client.list_git_remotes())
    if not dry_run:
        _repair_interrupted_untracked_remote_bookmarks(
            client=client,
            remote=remote,
            state_dir=state_store.require_writable(),
        )
    stack = client.discover_review_stack(revset)
    if on_prepared is not None:
        on_prepared(
            stack.head.change_id,
            stack.head.subject,
        )
    state = state_store.load()
    bookmark_states = client.list_bookmark_states()
    matched_bookmarks = match_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        patterns=use_bookmarks,
        revisions=stack.revisions,
        remote_name=remote.name,
    )
    discovered_bookmarks = discover_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        prefix=config.bookmark_prefix,
        remote_name=remote.name,
        revisions=stack.revisions,
    )
    bookmark_result = BookmarkResolver(
        state,
        prefix=config.bookmark_prefix,
        matched_bookmarks=matched_bookmarks,
        discovered_bookmarks=discovered_bookmarks,
    ).pin_revisions(stack.revisions)
    ensure_unique_bookmarks(bookmark_result.resolutions)
    _preflight_conflicted_revisions(stack.revisions)
    _preflight_private_commits(client, stack.revisions)
    (
        generated_pull_request_descriptions,
        generated_stack_description,
    ) = resolve_generated_descriptions(
        describe_with=describe_with,
        jj_client=client,
        selected_revset=stack.selected_revset,
        revisions=stack.revisions,
    )
    return PreparedSubmitInputs(
        bookmark_states=bookmark_states,
        bookmark_result=bookmark_result,
        client=client,
        generated_pull_request_descriptions=generated_pull_request_descriptions,
        generated_stack_description=generated_stack_description,
        remote=remote,
        stack=stack,
        state=state,
    )


def _start_submit_intent(
    *,
    bookmark_result: BookmarkResolutionResult,
    dry_run: bool,
    github_repository,
    remote_name: str,
    stack: LocalStack,
    state_store: ReviewStateStore,
) -> SubmitIntentState:
    """Prepare submit intent state before any remote mutation begins."""

    ordered_change_ids = tuple(revision.change_id for revision in stack.revisions)
    ordered_commit_ids = tuple(revision.commit_id for revision in stack.revisions)
    intent = SubmitIntent(
        kind="submit",
        pid=os.getpid(),
        label=(
            f"submit for {short_change_id(stack.head.change_id)} (from {stack.selected_revset})"
        ),
        display_revset=stack.selected_revset,
        ordered_commit_ids=ordered_commit_ids,
        remote_name=remote_name,
        github_host=github_repository.host,
        github_owner=github_repository.owner,
        github_repo=github_repository.repo,
        ordered_change_ids=ordered_change_ids,
        bookmarks={
            revision.change_id: resolution.bookmark
            for revision, resolution in zip(
                stack.revisions,
                bookmark_result.resolutions,
                strict=True,
            )
        },
        started_at=datetime.now(UTC).isoformat(),
    )
    if dry_run:
        stale_intents = _list_stale_submit_intents_without_waiting(
            state_store=state_store,
            intent=intent,
        )
        _report_stale_submit_intents(
            current_intent=intent,
            ordered_change_ids=ordered_change_ids,
            ordered_commit_ids=ordered_commit_ids,
            stale_intents=stale_intents,
        )
        return SubmitIntentState(intent=intent, intent_path=None, stale_intents=stale_intents)

    state_dir = state_store.require_writable()
    stale_intents = check_same_kind_intent(state_dir, intent)
    _report_stale_submit_intents(
        current_intent=intent,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        stale_intents=stale_intents,
    )
    return SubmitIntentState(
        intent=intent,
        intent_path=write_new_intent(state_dir, intent),
        stale_intents=stale_intents,
    )


def _report_stale_submit_intents(
    *,
    current_intent: SubmitIntent,
    ordered_change_ids: tuple[str, ...],
    ordered_commit_ids: tuple[str, ...],
    stale_intents: list[LoadedIntent],
) -> None:
    """Render resumable submit intent diagnostics for the operator."""

    for loaded in stale_intents:
        if not isinstance(loaded.intent, SubmitIntent):
            continue
        decision = submit_status_decision(
            intent=loaded.intent,
            current_change_ids=ordered_change_ids,
            current_commit_ids=ordered_commit_ids,
            current_identity=SubmitRecoveryIdentity.from_intent(current_intent),
        )
        description = describe_intent(loaded.intent)
        if decision is SubmitStatusDecision.CONTINUE:
            console.note(t"Continuing interrupted {description}", soft_wrap=True)
        elif decision is SubmitStatusDecision.CURRENT_STACK:
            console.note(
                t"Note: interrupted {description} does not match the current stack "
                t"exactly. This submit will use the current stack.",
                soft_wrap=True,
            )
        elif decision is SubmitStatusDecision.INSPECT:
            console.note(
                t"Note: interrupted {description} matches the current stack, "
                t"but its recorded submit target does not. This submit will use "
                t"the current stack.",
                soft_wrap=True,
            )
        else:
            console.note(
                t"Note: incomplete operation outstanding: {description}",
                soft_wrap=True,
            )


def _prepare_submit_revisions(
    *,
    bookmark_result: BookmarkResolutionResult,
    bookmark_states: dict[str, BookmarkState],
    client: JjClient,
    dry_run: bool,
    remote: GitRemote,
    stack: LocalStack,
) -> tuple[PreparedSubmitRevision, ...]:
    """Resolve bookmark mutations and push strategy for each stack revision."""

    prepared_revisions: list[PreparedSubmitRevision] = []
    actual_remote_targets = _load_actual_remote_targets_for_saved_bookmarks(
        bookmark_result=bookmark_result,
        client=client,
        remote=remote,
        stack=stack,
    )
    _preflight_actual_remote_targets(
        actual_remote_targets=actual_remote_targets,
        bookmark_result=bookmark_result,
        remote=remote,
        stack=stack,
    )
    for resolution, revision in zip(
        bookmark_result.resolutions,
        stack.revisions,
        strict=True,
    ):
        _ensure_change_is_not_unlinked(
            cached_change=bookmark_result.state.changes.get(revision.change_id),
            change_id=revision.change_id,
        )
        bookmark_state = bookmark_states.get(
            resolution.bookmark,
            BookmarkState(name=resolution.bookmark),
        )
        local_action = _resolve_local_action(
            resolution.bookmark,
            bookmark_state.local_targets,
            revision.commit_id,
        )
        remote_state = bookmark_state.remote_target(remote.name)
        _ensure_remote_can_be_updated(
            bookmark=resolution.bookmark,
            bookmark_source=resolution.source,
            bookmark_state=bookmark_state,
            change_id=revision.change_id,
            desired_target=revision.commit_id,
            remote=remote.name,
            remote_state=remote_state,
            state=bookmark_result.state,
        )

        if local_action != "unchanged" and not dry_run:
            allow_backwards = _bookmark_is_already_managed_for_change(
                bookmark=resolution.bookmark,
                bookmark_state=bookmark_state,
                cached_change=bookmark_result.state.changes.get(revision.change_id),
                change_id=revision.change_id,
                jj_client=client,
            )
            client.set_bookmark(
                resolution.bookmark,
                revision.commit_id,
                allow_backwards=allow_backwards,
            )

        expected_remote_target: str | None = None
        if remote_state is not None and remote_state.target == revision.commit_id:
            push_operation: PushOperation = "up_to_date"
            remote_action: RemoteBookmarkAction = "up to date"
        elif (
            remote_state is not None
            and not remote_state.is_tracked
            and len(remote_state.targets) == 1
            and remote_state.target != revision.commit_id
        ):
            if remote_state is None:
                raise AssertionError("Checked remote bookmark state must exist.")
            expected_remote_target = remote_state.target
            if expected_remote_target is None:
                raise AssertionError("Checked remote target must be unambiguous.")
            push_operation = "git_update"
            remote_action = "pushed"
        else:
            push_operation = "batch"
            remote_action = "pushed"

        prepared_revisions.append(
            PreparedSubmitRevision(
                bookmark=resolution.bookmark,
                bookmark_source=resolution.source,
                change_id=revision.change_id,
                expected_remote_target=expected_remote_target,
                local_action=local_action,
                push_operation=push_operation,
                remote_action=remote_action,
                revision=revision,
            )
        )
    return tuple(prepared_revisions)


def _load_actual_remote_targets_for_saved_bookmarks(
    *,
    bookmark_result: BookmarkResolutionResult,
    client: JjClient,
    remote: GitRemote,
    stack: LocalStack,
) -> dict[str, str]:
    bookmarks = tuple(
        sorted(
            {
                resolution.bookmark
                for resolution, revision in zip(
                    bookmark_result.resolutions,
                    stack.revisions,
                    strict=True,
                )
                if _cached_change_has_saved_remote_target(
                    bookmark_result.state.changes.get(revision.change_id),
                    resolution.bookmark,
                )
            }
        )
    )
    if not bookmarks:
        return {}
    return client.list_remote_branches(
        remote=remote.name,
        patterns=tuple(f"refs/heads/{bookmark}" for bookmark in bookmarks),
    )


def _preflight_actual_remote_targets(
    *,
    actual_remote_targets: dict[str, str],
    bookmark_result: BookmarkResolutionResult,
    remote: GitRemote,
    stack: LocalStack,
) -> None:
    for resolution, revision in zip(
        bookmark_result.resolutions,
        stack.revisions,
        strict=True,
    ):
        _ensure_actual_remote_target_is_safe(
            actual_remote_targets=actual_remote_targets,
            bookmark=resolution.bookmark,
            cached_change=bookmark_result.state.changes.get(revision.change_id),
            desired_target=revision.commit_id,
            remote=remote.name,
        )


def _cached_change_has_saved_remote_target(
    cached_change: CachedChange | None,
    bookmark: str,
) -> bool:
    return (
        cached_change is not None
        and not cached_change.is_unlinked
        and cached_change.bookmark == bookmark
        and cached_change.last_submitted_commit_id is not None
    )


def _ensure_actual_remote_target_is_safe(
    *,
    actual_remote_targets: dict[str, str],
    bookmark: str,
    cached_change: CachedChange | None,
    desired_target: str,
    remote: str,
) -> None:
    if not _cached_change_has_saved_remote_target(cached_change, bookmark):
        return
    if cached_change is None:
        raise AssertionError("Checked cached change must exist.")
    saved_target = cached_change.last_submitted_commit_id
    if saved_target is None:
        raise AssertionError("Checked cached change must have a saved submitted commit.")
    actual_target = actual_remote_targets.get(bookmark)
    if actual_target in {saved_target, desired_target}:
        return
    if actual_target is None:
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} no longer exists.",
            hint=(
                t"Fetch and inspect the PR link before submitting again. If this branch "
                t"should stay attached to this change, repair the link with relink."
            ),
        )
    raise CliError(
        t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} points to an "
        t"unexpected commit.",
        hint=(
            t"Fetch and inspect the PR link before submitting again. If this branch "
            t"should stay attached to this change, repair the link with relink."
        ),
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
        if cached_change is not None and cached_change.has_review_identity:
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


async def _run_submit_async(
    *,
    config: RepoConfig,
    describe_with: str | None,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    jj_client: JjClient,
    labels: list[str] | None,
    on_prepared: Callable[[str, str], None] | None,
    re_request: bool,
    revset: str | None,
    reviewers: list[str] | None,
    state_store: ReviewStateStore,
    team_reviewers: list[str] | None,
    use_bookmarks: list[str] | None,
) -> SubmitResult:
    resolved_use_bookmarks = config.use_bookmarks if use_bookmarks is None else use_bookmarks
    with console.spinner(description="Preparing submit"):
        prepared_inputs = _prepare_submit_inputs(
            config=config,
            describe_with=describe_with,
            dry_run=dry_run,
            jj_client=jj_client,
            on_prepared=on_prepared,
            revset=revset,
            state_store=state_store,
            use_bookmarks=tuple(resolved_use_bookmarks),
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
    resolved_labels = config.labels if labels is None else labels
    resolved_reviewers = config.reviewers if reviewers is None else reviewers
    resolved_team_reviewers = config.team_reviewers if team_reviewers is None else team_reviewers
    prepared_revisions = _prepare_submit_revisions(
        bookmark_result=bookmark_result,
        bookmark_states=bookmark_states,
        client=client,
        dry_run=dry_run,
        remote=remote,
        stack=stack,
    )
    state_changes = dict(bookmark_result.state.changes)
    intent_state = _start_submit_intent(
        bookmark_result=bookmark_result,
        dry_run=dry_run,
        github_repository=github_repository,
        remote_name=remote.name,
        stack=stack,
        state_store=state_store,
    )
    if dry_run:
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
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            with console.spinner(description="Inspecting GitHub"):
                try:
                    github_repository_state, discovered_pull_requests = await asyncio.gather(
                        github_client.get_repository(
                            github_repository.owner,
                            github_repository.repo,
                        ),
                        _discover_pull_requests_by_bookmark(
                            github_client=github_client,
                            github_repository=github_repository,
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
            if not dry_run and any(
                revision.remote_action == "pushed" for revision in prepared_revisions
            ):
                await _retarget_review_bases_before_branch_push(
                    bookmark_states=bookmark_states,
                    github_client=github_client,
                    github_repository=github_repository,
                    jj_client=client,
                    pending_syncs=pending_syncs,
                    prepared_revisions=prepared_revisions,
                    remote_name=remote.name,
                    trunk_branch=trunk_branch,
                )
                with console.spinner(description="Pushing review branches"):
                    _sync_remote_bookmarks(
                        client=client,
                        dry_run=dry_run,
                        prepared_revisions=prepared_revisions,
                        remote=remote,
                    )
            else:
                _sync_remote_bookmarks(
                    client=client,
                    dry_run=dry_run,
                    prepared_revisions=prepared_revisions,
                    remote=remote,
                )
            with console.progress(
                description="Syncing pull requests",
                total=len(prepared_revisions),
            ) as progress:
                submitted_revisions = await _sync_pull_requests(
                    draft_mode=draft_mode,
                    dry_run=dry_run,
                    github_client=github_client,
                    github_repository=github_repository,
                    labels=resolved_labels,
                    on_progress=progress.advance,
                    pending_syncs=pending_syncs,
                    re_request=re_request,
                    reviewers=resolved_reviewers,
                    state=bookmark_result.state,
                    state_changes=state_changes,
                    state_store=state_store,
                    team_reviewers=resolved_team_reviewers,
                )

            if not dry_run:
                await sync_stack_comments(
                    concurrency=_GITHUB_INSPECTION_CONCURRENCY,
                    dry_run=dry_run,
                    generated_stack_description=prepared_inputs.generated_stack_description,
                    github_client=github_client,
                    github_repository=github_repository,
                    revisions=submitted_revisions,
                    state=bookmark_result.state,
                    state_changes=state_changes,
                    state_store=state_store,
                    trunk_branch=trunk_branch,
                )
                await _verify_no_unexpected_pull_request_closures(
                    discovered_pull_requests=discovered_pull_requests,
                    github_client=github_client,
                    github_repository=github_repository,
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
        if succeeded and intent_state.intent_path is not None:
            retire_superseded_intents(intent_state.stale_intents, intent_state.intent)
            intent_state.intent_path.unlink(missing_ok=True)


def _list_stale_submit_intents_without_waiting(
    *,
    state_store: ReviewStateStore,
    intent: SubmitIntent,
) -> list[LoadedIntent]:
    return [
        loaded
        for loaded in state_store.list_intents()
        if loaded.intent.kind == intent.kind and not pid_is_alive(loaded.intent.pid)
    ]


def _repair_interrupted_untracked_remote_bookmarks(
    *,
    client: InterruptedRemoteBookmarkRepairer,
    remote: GitRemote,
    state_dir: Path,
) -> None:
    current_github_repository = parse_github_repo(remote)
    if current_github_repository is None:
        return

    stale_submit_intents: list[SubmitIntent] = []
    for loaded in scan_intents(state_dir):
        intent = loaded.intent
        if not isinstance(intent, SubmitIntent):
            continue
        if pid_is_alive(intent.pid):
            continue
        if intent.remote_name != remote.name:
            continue
        if (
            intent.github_host,
            intent.github_owner,
            intent.github_repo,
        ) != (
            current_github_repository.host,
            current_github_repository.owner,
            current_github_repository.repo,
        ):
            continue
        stale_submit_intents.append(intent)

    if not stale_submit_intents:
        return

    bookmarks = tuple(
        sorted(
            {
                bookmark
                for loaded in stale_submit_intents
                for bookmark in loaded.bookmarks.values()
            }
        )
    )
    if not bookmarks:
        return

    client.fetch_remote(remote=remote.name)
    bookmark_states = client.list_bookmark_states(bookmarks)
    for bookmark in bookmarks:
        bookmark_state = bookmark_states.get(bookmark)
        if bookmark_state is None:
            continue
        remote_state = bookmark_state.remote_target(remote.name)
        if remote_state is None or remote_state.is_tracked:
            continue
        local_target = bookmark_state.local_target
        if local_target is None or remote_state.target != local_target:
            continue
        client.track_bookmark(remote=remote.name, bookmark=bookmark)


def _bookmark_is_already_managed_for_change(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    cached_change: CachedChange | None,
    change_id: str,
    jj_client: JjClient,
) -> bool:
    """Whether `submit` is reasserting an already-managed bookmark for the same change.

    Same-change rewrites such as `jj split` can leave the bookmark pointing at a sibling
    of the desired commit (the other half of the split, or any post-rewrite commit that
    is not a descendant of the previous target). `jj bookmark set` refuses such
    "backwards or sideways" moves by default. The move is legitimate when the tool's
    saved state already records this bookmark as managed for this change, or when the
    bookmark's current local target itself resolves to the same logical change as the
    desired commit. In either case `allow_backwards` is correct. For any other case the
    default guard stays in effect so an unrelated bookmark cannot be silently
    retargeted.

    A hidden `local_target` (e.g., abandoned by the user manually) returns False on the
    same-change-id branch because `query_revisions` does not surface hidden revisions.
    That keeps the default guard in effect, which is the safer behavior: forcing the
    move would require recovering a hidden commit's identity that we cannot prove.
    """

    if (
        cached_change is not None
        and cached_change.manages_bookmark
        and cached_change.bookmark == bookmark
    ):
        return True
    local_target = bookmark_state.local_target
    if local_target is None:
        return False
    revisions = jj_client.query_revisions(f"'{local_target}'")
    return len(revisions) == 1 and revisions[0].change_id == change_id


def _resolve_local_action(
    bookmark: str,
    local_targets: tuple[str, ...],
    desired_target: str,
) -> LocalBookmarkAction:
    if len(local_targets) > 1:
        raise CliError(
            t"Bookmark {ui.bookmark(bookmark)} has {len(local_targets)} conflicting "
            t"local targets.",
            hint=t"Resolve the bookmark conflict with {ui.cmd('jj bookmark')} before submitting.",
        )
    local_target = local_targets[0] if local_targets else None
    if local_target == desired_target:
        return "unchanged"
    if local_target is None:
        return "created"
    return "moved"


def _ensure_remote_can_be_updated(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    desired_target: str,
    remote: str,
    remote_state: RemoteBookmarkState | None,
    state: ReviewState,
) -> None:
    if remote_state is None or not remote_state.targets:
        return
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} is conflicted. "
            t"Resolve it with {ui.cmd('jj git fetch')} and retry."
        )
    if remote_state.target == desired_target:
        return
    if _bookmark_link_is_proven(
        bookmark=bookmark,
        bookmark_source=bookmark_source,
        bookmark_state=bookmark_state,
        change_id=change_id,
        state=state,
    ):
        return
    raise CliError(
        t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} already exists and "
        t"points elsewhere. Submit will not take over an existing remote branch "
        t"unless its link is already proven by local state, tracking data, or "
        t"explicit relinking."
    )


def _bookmark_link_is_proven(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    state: ReviewState,
) -> bool:
    if bookmark_state.local_target is not None:
        return True
    if bookmark_source == "discovered":
        return True
    if bookmark_source != "saved":
        return False
    cached_change = state.changes.get(change_id)
    return (
        cached_change is not None
        and not cached_change.is_unlinked
        and cached_change.bookmark == bookmark
    )


def _preflight_private_commits(
    client: PrivateCommitFinder,
    revisions: tuple[LocalRevision, ...],
) -> None:
    private = client.find_private_commits(revisions)
    if not private:
        return
    subjects = ui.join(
        lambda revision: t"{ui.change_id(revision.change_id)} ({revision.subject})",
        private,
    )
    raise CliError(
        t"Stack contains commits blocked by "
        t"{ui.code('git.private-commits')}: {subjects}.",
        hint="Remove these changes from the stack before submitting.",
    )


def _preflight_conflicted_revisions(revisions: tuple[LocalRevision, ...]) -> None:
    conflicted = tuple(revision for revision in revisions if revision.conflict)
    if not conflicted:
        return
    subjects = ui.join(
        lambda revision: t"{ui.change_id(revision.change_id)} ({revision.subject})",
        conflicted,
    )
    raise CliError(
        t"Stack contains changes with unresolved conflicts: {subjects}. "
        t"Resolve these changes before submitting."
    )


async def _discover_pull_requests_by_bookmark(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    bookmarks: tuple[str, ...],
) -> dict[str, GithubPullRequest | None]:
    if not bookmarks:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=bookmarks,
        )
    except GithubClientError as error:
        raise CliError("Could not batch pull request discovery for branches") from error

    return {
        bookmark: _select_discovered_pull_request(
            head_label=f"{github_repository.owner}:{bookmark}",
            pull_requests=discovered_pull_requests.get(bookmark, ()),
        )
        for bookmark in bookmarks
    }


def _sync_remote_bookmarks(
    *,
    client: RemoteBookmarkSyncer,
    dry_run: bool,
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote: GitRemote,
) -> None:
    batch_push_bookmarks = tuple(
        prepared_revision.bookmark
        for prepared_revision in prepared_revisions
        if prepared_revision.push_operation == "batch"
    )
    if batch_push_bookmarks:
        if not dry_run:
            client.push_bookmarks(
                remote=remote.name,
                bookmarks=batch_push_bookmarks,
            )

    for prepared_revision in prepared_revisions:
        if prepared_revision.push_operation != "git_update":
            continue
        if not dry_run:
            if prepared_revision.expected_remote_target is None:
                raise AssertionError("Git remote update requires an expected target.")
            client.update_untracked_remote_bookmark(
                remote=remote.name,
                bookmark=prepared_revision.bookmark,
                desired_target=prepared_revision.revision.commit_id,
                expected_remote_target=prepared_revision.expected_remote_target,
            )


def _save_submit_state_checkpoint(
    *,
    dry_run: bool,
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
) -> None:
    if dry_run:
        return
    interim_state = state.model_copy(update={"changes": dict(state_changes)})
    state_store.save(interim_state)


async def _sync_pull_requests(
    *,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    pending_syncs: tuple[PendingPullRequestSync, ...],
    re_request: bool,
    reviewers: list[str],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    team_reviewers: list[str],
    on_progress: Callable[[], None] | None = None,
) -> tuple[SubmittedRevision, ...]:
    def handle_success(_index: int, submitted: SubmittedPullRequestSync) -> None:
        if submitted.cached_change is not None:
            state_changes[submitted.submitted_revision.change_id] = submitted.cached_change
        _save_submit_state_checkpoint(
            dry_run=dry_run,
            state=state,
            state_changes=state_changes,
            state_store=state_store,
        )
        if on_progress is not None:
            on_progress()

    submitted_revisions = await run_bounded_tasks(
        concurrency=_GITHUB_INSPECTION_CONCURRENCY,
        items=pending_syncs,
        run_item=lambda pending_sync: _sync_pull_request_task(
            draft_mode=draft_mode,
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            labels=labels,
            pending_sync=pending_sync,
            re_request=re_request,
            reviewers=reviewers,
            state=state,
            team_reviewers=team_reviewers,
        ),
        on_success=handle_success,
    )
    return tuple(submitted.submitted_revision for submitted in submitted_revisions)


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


async def _retarget_review_bases_before_branch_push(
    *,
    bookmark_states: dict[str, BookmarkState],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    jj_client: JjClient,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote_name: str,
    trunk_branch: str,
) -> None:
    """Move PR bases that would auto-close after the push to trunk first."""

    retarget_syncs = _predict_pull_requests_auto_closed_by_push(
        bookmark_states=bookmark_states,
        jj_client=jj_client,
        pending_syncs=pending_syncs,
        prepared_revisions=prepared_revisions,
        remote_name=remote_name,
    )
    await run_bounded_tasks(
        concurrency=_GITHUB_INSPECTION_CONCURRENCY,
        items=retarget_syncs,
        run_item=lambda pending_sync: _retarget_review_base_before_branch_push(
            github_client=github_client,
            github_repository=github_repository,
            pending_sync=pending_sync,
            trunk_branch=trunk_branch,
        ),
    )


def _predict_pull_requests_auto_closed_by_push(
    *,
    bookmark_states: dict[str, BookmarkState],
    jj_client: JjClient,
    pending_syncs: tuple[PendingPullRequestSync, ...],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote_name: str,
) -> tuple[PendingPullRequestSync, ...]:
    """Pending PRs that GitHub will auto-close (as merged) after the planned push.

    GitHub auto-closes an open PR when its head ref becomes contained in its base
    ref. The push moves head and (transitively, via stacked bookmarks) base, so
    the prediction is run against the post-push commit IDs each ref will hold.
    """

    push_targets = {
        prepared_revision.bookmark: prepared_revision.revision.commit_id
        for prepared_revision in prepared_revisions
    }
    candidates: list[tuple[str, str, PendingPullRequestSync]] = []
    for pending_sync in pending_syncs:
        pull_request = pending_sync.discovered_pull_request
        if pull_request is None or pull_request.state != "open":
            continue
        head_after_push = push_targets.get(pull_request.head.ref)
        if head_after_push is None:
            continue
        base_after_push = _resolve_post_push_commit(
            ref=pull_request.base.ref,
            push_targets=push_targets,
            bookmark_states=bookmark_states,
            remote_name=remote_name,
        )
        if base_after_push is None:
            continue
        candidates.append((head_after_push, base_after_push, pending_sync))

    if not candidates:
        return ()
    auto_close_heads = jj_client.query_paired_ancestor_membership(
        tuple((head, base) for head, base, _ in candidates),
    )
    return tuple(
        pending_sync
        for head, _, pending_sync in candidates
        if head in auto_close_heads
    )


def _resolve_post_push_commit(
    *,
    bookmark_states: dict[str, BookmarkState],
    push_targets: dict[str, str],
    ref: str,
    remote_name: str,
) -> str | None:
    """Resolve the commit ID a ref will point at after the planned push lands."""

    if ref in push_targets:
        return push_targets[ref]
    bookmark_state = bookmark_states.get(ref)
    if bookmark_state is None:
        return None
    remote_state = bookmark_state.remote_target(remote_name)
    if remote_state is None or remote_state.target is None:
        return None
    return remote_state.target


async def _retarget_review_base_before_branch_push(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pending_sync: PendingPullRequestSync,
    trunk_branch: str,
) -> None:
    pull_request = pending_sync.discovered_pull_request
    if pull_request is None:
        raise AssertionError("Pre-push retarget requires a discovered pull request.")
    try:
        await github_client.update_pull_request(
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
            t"{ui.bookmark(trunk_branch)} before pushing review branches"
        ) from error


async def _verify_no_unexpected_pull_request_closures(
    *,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
) -> None:
    """Detect open→closed transitions that happened during submit and raise loudly.

    `submit` never closes pull requests on purpose. Any PR that was open at the
    start of this run and is closed by the end means a GitHub destructive default
    fired in a way the pre-push predictor did not anticipate (typically the
    head-contained-in-base auto-close). Surface it loudly rather than persist
    state that hides the loss.
    """

    initially_open_numbers = sorted(
        {
            pull_request.number
            for pull_request in discovered_pull_requests.values()
            if pull_request is not None and pull_request.state == "open"
        }
    )
    if not initially_open_numbers:
        return
    try:
        refetched = await github_client.get_pull_requests_by_numbers(
            github_repository.owner,
            github_repository.repo,
            pull_numbers=initially_open_numbers,
        )
    except GithubClientError as error:
        raise CliError(
            "Could not refetch pull request states for the post-submit safety check"
        ) from error

    closed_numbers: list[int] = []
    for number in initially_open_numbers:
        pull_request = refetched.get(number)
        if pull_request is None:
            continue
        if pull_request.state != "open":
            closed_numbers.append(number)
    if not closed_numbers:
        return

    rendered_numbers = ", ".join(f"#{number}" for number in closed_numbers)
    raise CliError(
        t"Pull request(s) {rendered_numbers} were open at the start of this submit "
        t"but are closed by the end. GitHub closes a pull request automatically when "
        t"its head commit becomes reachable from the base branch during a push. "
        t"Reopen those pull requests on GitHub now to keep their existing reviews — "
        t"once reopened, rerunning {ui.cmd('jj-review submit')} is safe and will "
        t"restore the stacked bases."
    )


async def _sync_pull_request_task(
    *,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    pending_sync: PendingPullRequestSync,
    re_request: bool,
    reviewers: list[str],
    state: ReviewState,
    team_reviewers: list[str],
) -> SubmittedPullRequestSync:
    prepared_revision = pending_sync.prepared_revision
    pull_request_result = await _sync_pull_request(
        base_branch=pending_sync.base_branch,
        bookmark=prepared_revision.bookmark,
        bookmark_source=prepared_revision.bookmark_source,
        change_id=prepared_revision.change_id,
        discovered_pull_request=pending_sync.discovered_pull_request,
        draft_mode=draft_mode,
        dry_run=dry_run,
        generated_description=pending_sync.generated_description,
        github_client=github_client,
        github_repository=github_repository,
        labels=labels,
        parent_change_id=pending_sync.parent_change_id,
        re_request=re_request,
        reviewers=reviewers,
        revision=prepared_revision.revision,
        stack_head_change_id=pending_sync.stack_head_change_id,
        state=state,
        team_reviewers=team_reviewers,
    )
    return SubmittedPullRequestSync(
        cached_change=pull_request_result.cached_change,
        submitted_revision=SubmittedRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            change_id=prepared_revision.change_id,
            commit_id=prepared_revision.revision.commit_id,
            local_action=prepared_revision.local_action,
            native_revision=prepared_revision.revision,
            pull_request_action=pull_request_result.action,
            pull_request_is_draft=(
                pull_request_result.pull_request.is_draft
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_number=(
                pull_request_result.pull_request.number
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_title=(
                pull_request_result.pull_request.title
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_url=(
                pull_request_result.pull_request.html_url
                if pull_request_result.pull_request is not None
                else None
            ),
            remote_action=prepared_revision.remote_action,
            subject=prepared_revision.revision.subject,
        ),
    )


async def _sync_pull_request(
    *,
    base_branch: str,
    bookmark: str,
    bookmark_source: BookmarkSource,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    generated_description: GeneratedDescription,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    parent_change_id: str | None,
    re_request: bool,
    reviewers: list[str],
    revision: LocalRevision,
    stack_head_change_id: str | None,
    state: ReviewState,
    team_reviewers: list[str],
) -> PullRequestSyncResult:
    cached_change = state.changes.get(change_id)
    _ensure_pull_request_link_is_consistent(
        bookmark=bookmark,
        cached_change=cached_change,
        change_id=change_id,
        discovered_pull_request=discovered_pull_request,
    )

    title = generated_description.title
    body = generated_description.body
    if discovered_pull_request is None:
        pull_request = None
        if not dry_run:
            pull_request = await _create_pull_request(
                base_branch=base_branch,
                body=body,
                draft=(draft_mode in ("draft", "draft_all")),
                github_client=github_client,
                github_repository=github_repository,
                head_branch=bookmark,
                title=title,
            )
        action: PullRequestAction = "created"
    elif (
        discovered_pull_request.base.ref == base_branch
        and (discovered_pull_request.body or "") == body
        and discovered_pull_request.title == title
    ):
        pull_request = discovered_pull_request
        action = "unchanged"
    else:
        pull_request = discovered_pull_request
        if not dry_run:
            pull_request = await _update_pull_request(
                base_branch=base_branch,
                body=body,
                github_client=github_client,
                github_repository=github_repository,
                pull_request=discovered_pull_request,
                title=title,
            )
        action = "updated"

    if pull_request is not None and pull_request.state == "open":
        if draft_mode == "publish" and pull_request.is_draft:
            if not dry_run:
                pull_request = await _mark_pull_request_ready_for_review(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"
        elif draft_mode == "draft_all" and not pull_request.is_draft:
            if not dry_run:
                pull_request = await _convert_pull_request_to_draft(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"

    if (
        not dry_run
        and pull_request is not None
        and _should_sync_pull_request_metadata(
            action=action,
            cached_change=cached_change,
            re_request=False,
        )
    ):
        await _sync_pull_request_metadata(
            github_client=github_client,
            github_repository=github_repository,
            labels=labels,
            pull_request_number=pull_request.number,
            reviewers=reviewers,
            team_reviewers=team_reviewers,
        )

    if not dry_run and re_request and pull_request is not None:
        re_request_reviewers = await _load_re_request_reviewers(
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pull_request.number,
        )
        merged_reviewers = _merge_re_request_reviewers(
            reviewers=reviewers,
            re_request_reviewers=re_request_reviewers,
        )
        if merged_reviewers != reviewers:
            await _sync_pull_request_metadata(
                github_client=github_client,
                github_repository=github_repository,
                labels=[],
                pull_request_number=pull_request.number,
                reviewers=merged_reviewers,
                team_reviewers=[],
            )

    next_cached_change: CachedChange | None = None
    if pull_request is not None:
        next_cached_change = _updated_cached_change(
            bookmark=bookmark,
            bookmark_source=bookmark_source,
            cached_change=cached_change,
            commit_id=revision.commit_id,
            parent_change_id=parent_change_id,
            pull_request=pull_request,
            stack_head_change_id=stack_head_change_id,
        )
    return PullRequestSyncResult(
        action=action,
        cached_change=next_cached_change,
        pull_request=pull_request,
    )


def _should_sync_pull_request_metadata(
    *,
    action: PullRequestAction,
    cached_change: CachedChange | None,
    re_request: bool,
) -> bool:
    if re_request:
        return True
    if action != "unchanged":
        return True
    if cached_change is None:
        return True
    return cached_change.pr_number is None and cached_change.pr_url is None


async def _load_re_request_reviewers(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
) -> list[str]:
    try:
        reviews = await github_client.list_pull_request_reviews(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not load reviews for pull request #{pull_request_number}"
        ) from error
    return _reviewers_to_re_request(reviews)


def _reviewers_to_re_request(
    reviews: Sequence[GithubPullRequestReview],
) -> list[str]:
    latest_reviews_by_user: dict[str, GithubPullRequestReview] = {}
    for review in sorted(reviews, key=lambda item: item.id):
        reviewer = review.user
        if reviewer is None:
            continue
        normalized_state = review.state.upper()
        if normalized_state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        latest_reviews_by_user[reviewer.login] = review

    selected_reviews = sorted(
        (
            review
            for review in latest_reviews_by_user.values()
            if review.state.upper() in {"APPROVED", "CHANGES_REQUESTED"}
        ),
        key=lambda item: item.id,
    )
    return [review.user.login for review in selected_reviews if review.user is not None]


def _merge_re_request_reviewers(
    *,
    reviewers: list[str],
    re_request_reviewers: list[str],
) -> list[str]:
    merged = list(reviewers)
    seen = set(reviewers)
    for reviewer in re_request_reviewers:
        if reviewer in seen:
            continue
        seen.add(reviewer)
        merged.append(reviewer)
    return merged


def _select_discovered_pull_request(
    *,
    head_label: str,
    pull_requests: tuple[GithubPullRequest, ...],
) -> GithubPullRequest | None:
    if len(pull_requests) > 1:
        raise CliError(
            t"GitHub reports multiple pull requests for head branch "
            t"{ui.bookmark(head_label)}.",
            hint=(
                t"Inspect the PR link with {ui.cmd('status --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if not pull_requests:
        return None
    pull_request = pull_requests[0]
    if pull_request.state != "open":
        raise CliError(
            t"GitHub reports pull request #{pull_request.number} for head branch "
            t"{ui.bookmark(head_label)} in state {pull_request.state}.",
            hint=(
                t"Inspect the PR link with {ui.cmd('status --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    return pull_request


def _ensure_pull_request_link_is_consistent(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
) -> None:
    _ensure_change_is_not_unlinked(
        cached_change=cached_change,
        change_id=change_id,
    )
    if cached_change is None or (
        cached_change.pr_number is None and cached_change.pr_url is None
    ):
        return
    if discovered_pull_request is None:
        raise CliError(
            t"Saved pull request link exists for bookmark {ui.bookmark(bookmark)}, "
            t"but GitHub no longer reports a PR for that head branch.",
            hint=(
                t"Inspect the PR link with {ui.cmd('status --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if cached_change.pr_number not in (None, discovered_pull_request.number):
        raise CliError(
            t"Saved pull request #{cached_change.pr_number} does not match the PR "
            t"GitHub reports for bookmark {ui.bookmark(bookmark)} "
            t"(#{discovered_pull_request.number}).",
            hint=(
                t"Inspect the PR link with {ui.cmd('status --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if cached_change.pr_url not in (None, discovered_pull_request.html_url):
        raise CliError(
            t"Saved pull request URL for bookmark {ui.bookmark(bookmark)} does not "
            t"match GitHub.",
            hint=(
                t"Inspect the PR link with {ui.cmd('status --fetch')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )


def _ensure_change_is_not_unlinked(
    *,
    cached_change: CachedChange | None,
    change_id: str,
) -> None:
    if cached_change is None or not cached_change.is_unlinked:
        return
    raise CliError(
        t"Change {ui.change_id(change_id)} is unlinked from review tracking.",
        hint=t"Run {ui.cmd('relink')} to reattach it before submitting again.",
    )


async def _create_pull_request(
    *,
    base_branch: str,
    body: str,
    draft: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    head_branch: str,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.create_pull_request(
            github_repository.owner,
            github_repository.repo,
            base=base_branch,
            body=body,
            draft=draft,
            head=head_branch,
            title=title,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not create a pull request for branch {ui.bookmark(head_branch)}"
        ) from error


async def _sync_pull_request_metadata(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    pull_request_number: int,
    reviewers: list[str],
    team_reviewers: list[str],
) -> None:
    try:
        if reviewers or team_reviewers:
            await github_client.request_reviewers(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if labels:
            await github_client.add_labels(
                github_repository.owner,
                github_repository.repo,
                issue_number=pull_request_number,
                labels=labels,
            )
    except GithubClientError as error:
        raise CliError(
            f"Could not synchronize metadata for pull request #{pull_request_number}"
        ) from error


async def _mark_pull_request_ready_for_review(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.mark_pull_request_ready_for_review(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}"
        ) from error


async def _convert_pull_request_to_draft(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.convert_pull_request_to_draft(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}"
        ) from error


async def _update_pull_request(
    *,
    base_branch: str,
    body: str,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.update_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request.number,
            base=base_branch,
            body=body,
            title=title,
        )
    except GithubClientError as error:
        raise CliError(f"Could not update pull request #{pull_request.number}") from error


def _updated_cached_change(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    cached_change: CachedChange | None,
    commit_id: str,
    parent_change_id: str | None,
    pull_request: GithubPullRequest,
    stack_head_change_id: str | None,
) -> CachedChange:
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            bookmark_ownership=bookmark_ownership_for_source(bookmark_source),
            last_submitted_commit_id=commit_id,
            last_submitted_parent_change_id=parent_change_id,
            last_submitted_stack_head_change_id=stack_head_change_id,
            pr_is_draft=pull_request.is_draft,
            pr_number=pull_request.number,
            pr_state=pull_request.state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": bookmark_ownership_for_source(bookmark_source),
            "last_submitted_commit_id": commit_id,
            "last_submitted_parent_change_id": parent_change_id,
            "last_submitted_stack_head_change_id": stack_head_change_id,
            "pr_is_draft": pull_request.is_draft,
            "pr_number": pull_request.number,
            "pr_state": pull_request.state,
            "pr_url": pull_request.html_url,
        }
    )
