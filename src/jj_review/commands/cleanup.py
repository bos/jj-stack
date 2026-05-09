"""Find and remove stale tracking data and review branches left behind by earlier review work.

By default, this runs a repo-wide cleanup of tracking data and review branches that no longer
match an active review. With `--rebase [REVSET]`, it works on one local stack instead.

Open orphaned PRs are preserved. Run `jj-review list` to see them, then retire one explicitly
with `jj-review close --cleanup --pull-request <pr>`.

Use `cleanup --rebase` when some changes from your stack have been merged on GitHub as rewritten
commits (e.g. via a squash merge in the GitHub UI). In this case, your local stack still
contains the old pre-merge commits, and `cleanup --rebase` will drop those merged ancestors from
the local stack and rebase the remaining local changes onto the current `trunk()`.

Use `--dry-run` to preview cleanup actions without making any changes.

"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.commands._close_actions import comment_matches_kind as _comment_matches_kind
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_review.config import RepoConfig
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.error_messages import (
    github_unavailable_message,
    remote_unavailable_message,
)
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    select_submit_remote,
)
from jj_review.github.stack_comments import (
    StackCommentKind,
    stack_comment_label,
)
from jj_review.jj import JjCliArgs, JjClient
from jj_review.jj.client import UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.github import GithubIssueComment
from jj_review.models.intent import CleanupIntent, CleanupRebaseIntent, LoadedIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.bookmarks import bookmark_glob, is_review_bookmark
from jj_review.review.change_status import classify_review_status_revision
from jj_review.review.intents import (
    describe_intent,
    match_cleanup_rebase_intent,
    retire_superseded_intents,
)
from jj_review.review.selection import resolve_selected_revset
from jj_review.review.status import (
    PreparedStatus,
    ReviewStatusRevision,
    prepare_status,
    status_preparation_cli_error,
    stream_status,
)
from jj_review.review.topology import is_open_pr_record
from jj_review.state.intents import check_same_kind_intent, write_new_intent
from jj_review.state.store import ReviewStateStore
from jj_review.ui import Message, plain_text

HELP = "Remove stale tracking data and review branches; optionally rebase one stack"

CleanupActionStatus = Literal["applied", "blocked", "planned", "skipped"]
type StackCommentCleanupEligibility = Literal["inspect", "needs-remote-check", "skip"]
type CleanupBody = Message
_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """One cleanup action that was planned, applied, blocked, or skipped."""

    kind: str
    status: CleanupActionStatus
    body: CleanupBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Rendered cleanup result for the selected repository."""

    actions: tuple[CleanupAction, ...]


@dataclass(frozen=True, slots=True)
class PreparedCleanup:
    """Locally prepared cleanup inputs before any GitHub inspection."""

    config: RepoConfig
    dry_run: bool
    bookmark_states: dict[str, BookmarkState]
    github_repository: ParsedGithubRepo | None
    github_repository_error: ErrorMessage | None
    jj_client: JjClient
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    remote_context_loaded: bool
    state: ReviewState
    state_store: ReviewStateStore


@dataclass(frozen=True, slots=True)
class StackCommentCleanupPlan:
    """Planned or blocked stack-comment cleanup details."""

    actions: tuple[CleanupAction, ...]
    comments: tuple[tuple[int, StackCommentKind], ...] = ()


@dataclass(frozen=True, slots=True)
class RemoteBranchCleanupPlan:
    """Planned or blocked remote-branch cleanup details."""

    action: CleanupAction
    expected_remote_target: str | None = None


@dataclass(frozen=True, slots=True)
class OrphanLocalBookmarkCleanupPlan:
    """Planned or blocked cleanup for one untracked local review bookmark."""

    action: CleanupAction
    bookmark: str


@dataclass(frozen=True, slots=True)
class PreparedCleanupChange:
    """Locally prepared cleanup state for one cached change."""

    bookmark_state: BookmarkState
    cached_change: CachedChange
    change_id: str
    inspect_stack_comment: bool
    stale_reason: str | None


@dataclass(frozen=True, slots=True)
class _StaleCleanupMutationPlan:
    """Planned local bookmark and remote branch mutations for one stale change."""

    cached_change: CachedChange
    local_bookmark_action: CleanupAction | None
    remote_plan: RemoteBranchCleanupPlan | None


@dataclass(frozen=True, slots=True)
class RebaseResult:
    """Rendered rebase result for one selected local stack."""

    actions: tuple[CleanupAction, ...]
    blocked: bool


@dataclass(frozen=True, slots=True)
class PreparedRebase:
    """Locally prepared rebase inputs before any rewrite."""

    config: RepoConfig
    dry_run: bool
    prepared_status: PreparedStatus


@dataclass(frozen=True, slots=True)
class _RebaseOperationPlan:
    """Derived rebase planning data before preview/live rendering."""

    blocked: bool
    closed_unmerged_revisions: tuple[ReviewStatusRevision, ...]
    merged_revisions: tuple[ReviewStatusRevision, ...]
    pre_actions: tuple[CleanupAction, ...]
    rebase_plans: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class _RebaseIntentState:
    """Prepared rebase intent bookkeeping for resumable live runs."""

    intent: CleanupRebaseIntent | None
    intent_path: Path | None
    stale_intents: list[LoadedIntent]


def _render_cleanup_action_header(*, dry_run: bool) -> str:
    """Render the cleanup action section header."""

    return "Planned cleanup actions:" if dry_run else "Applied cleanup actions:"


def _render_cleanup_postamble(*, result: CleanupResult) -> tuple[str, ...]:
    """Render cleanup lines that only depend on the completed result."""

    if not result.actions:
        return ("No cleanup actions needed.",)
    return ()


def _render_rebase_preamble(*, prepared_rebase: PreparedRebase) -> tuple[tuple[str, str], ...]:
    """Render the non-streaming rebase context lines for the CLI."""

    prepared_status = prepared_rebase.prepared_status
    prepared = prepared_status.prepared
    return _render_remote_and_github_lines(
        remote=prepared.remote,
        remote_error=prepared.remote_error,
        github_repository=(
            prepared_status.github_repository.full_name
            if prepared_status.github_repository is not None
            else None
        ),
        github_error=prepared_status.github_repository_error,
    )


def _render_rebase_action_header(*, dry_run: bool) -> str:
    """Render the rebase action section header."""

    return "Planned rebase actions:" if dry_run else "Applied rebase actions:"


def _render_rebase_postamble(*, result: RebaseResult) -> tuple[str, ...]:
    """Render rebase lines that only depend on the completed result."""

    if not result.actions:
        return ("No merged changes on the selected stack need rebasing.",)
    return ()


def cleanup(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
    rebase_revset: str | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    if rebase_revset is not None:
        return _run_cleanup_rebase_command(
            dry_run=dry_run,
            config=context.config,
            jj_client=context.jj_client,
            revset=rebase_revset,
        )

    return _run_cleanup_command(
        config=context.config,
        dry_run=dry_run,
        jj_client=context.jj_client,
    )


def _run_cleanup_rebase_command(
    *,
    dry_run: bool,
    config: RepoConfig,
    jj_client: JjClient,
    revset: str | None,
) -> int:
    """Render and run the `cleanup --rebase` command path."""

    selected_revset = resolve_selected_revset(
        command_label="cleanup --rebase --dry-run" if dry_run else "cleanup --rebase",
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )
    try:
        with console.spinner(description="Inspecting jj stack"):
            prepared_rebase = PreparedRebase(
                config=config,
                dry_run=dry_run,
                prepared_status=prepare_status(
                    config=config,
                    fetch_remote_state=True,
                    fetch_only_when_tracked=True,
                    jj_client=jj_client,
                    revset=selected_revset,
                ),
            )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    for severity, line in _render_rebase_preamble(prepared_rebase=prepared_rebase):
        if severity == "warning":
            console.warning(line)
        else:
            console.output(line)

    try:
        result = _stream_rebase(
            on_action=_build_action_streamer(
                dry_run=prepared_rebase.dry_run,
                render_header=_render_rebase_action_header,
            ),
            prepared_rebase=prepared_rebase,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    for line in _render_rebase_postamble(result=result):
        console.output(line)
    return 1 if result.blocked else 0


def _run_cleanup_command(
    *,
    config: RepoConfig,
    dry_run: bool,
    jj_client: JjClient,
) -> int:
    """Render and run the stale cleanup command path."""

    with console.spinner(description="Loading bookmark state"):
        prepared_cleanup = _prepare_cleanup(
            config=config,
            dry_run=dry_run,
            jj_client=jj_client,
        )
    stale_reasons = _stale_change_reasons(
        change_ids=tuple(prepared_cleanup.state.changes),
        jj_client=prepared_cleanup.jj_client,
    )
    if _cleanup_needs_remote_context(
        prepared_cleanup=prepared_cleanup,
        stale_reasons=stale_reasons,
    ):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        for severity, line in _render_remote_and_github_lines(
            remote=prepared_cleanup.remote,
            remote_error=prepared_cleanup.remote_error,
            github_repository=(
                prepared_cleanup.github_repository.full_name
                if prepared_cleanup.github_repository is not None
                else None
            ),
            github_error=prepared_cleanup.github_repository_error,
        ):
            if severity == "warning":
                console.warning(line)
            else:
                console.output(line)

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                dry_run=prepared_cleanup.dry_run,
                render_header=_render_cleanup_action_header,
            ),
            prepared_cleanup=prepared_cleanup,
            stale_reasons=stale_reasons,
        )
    )
    for line in _render_cleanup_postamble(result=result):
        console.output(line)
    return 0


def _build_action_streamer(
    *,
    dry_run: bool,
    render_header: Callable[..., str],
) -> Callable[[CleanupAction], None]:
    """Print the action header once, then stream actions as they arrive."""

    header_printed = False

    def emit_action(action: CleanupAction) -> None:
        nonlocal header_printed
        if not header_printed:
            console.output(render_header(dry_run=dry_run))
            header_printed = True
        prefix, prefix_style, body_style = _action_presentation(action.status)
        body = action.body
        if action.kind != "tracking":
            body = (ui.semantic_text(action.kind, "prefix"), ": ", body)
        console.output(
            ui.prefixed_line(
                f"{prefix} ",
                body,
                message_labels=body_style,
                prefix_labels=prefix_style,
            )
        )

    return emit_action


def _render_remote_and_github_lines(
    *,
    remote: GitRemote | None,
    remote_error: ErrorMessage | None,
    github_repository: str | None,
    github_error: ErrorMessage | None,
) -> tuple[tuple[str, str], ...]:
    lines: list[tuple[str, str]] = []
    if remote is None:
        lines.append(
            ("warning", ui.plain_text(remote_unavailable_message(remote_error=remote_error)))
        )
    github_message = github_unavailable_message(
        github_error=github_error,
        github_repository=github_repository,
    )
    if github_message is not None:
        lines.append(("warning", plain_text(github_message)))
    return tuple(lines)


def _action_presentation(
    status: CleanupActionStatus,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    if status == "applied":
        return (
            "  ✓",
            ("signature status good",),
            None,
        )
    if status == "planned":
        return (
            "  ~",
            ("hint heading",),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ("error heading",),
            ("warning heading",),
        )
    if status == "skipped":
        return (
            "  -",
            ("hint heading",),
            None,
        )
    return ("  ?", None, None)


def _revision_label_template(revision: ReviewStatusRevision):
    return t"{revision.subject} ({ui.change_id(revision.change_id)})"


def _rebase_destination_template(destination_change_id: str | None):
    if destination_change_id is None:
        return ui.revset("trunk()")
    return ui.change_id(destination_change_id)


def _prepare_cleanup(
    *,
    config: RepoConfig,
    dry_run: bool,
    jj_client: JjClient,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    state_store = ReviewStateStore.for_repo(jj_client.repo_root)
    state = state_store.load()
    if not dry_run:
        state_store.require_writable()

    bookmark_states = _load_bookmark_states(
        prefix=config.bookmark_prefix,
        jj_client=jj_client,
        state=state,
    )

    return PreparedCleanup(
        config=config,
        dry_run=dry_run,
        bookmark_states=bookmark_states,
        github_repository=None,
        github_repository_error=None,
        jj_client=jj_client,
        remote=None,
        remote_error=None,
        remote_context_loaded=False,
        state=state,
        state_store=state_store,
    )


def _prepared_rebase_has_potential_work(*, prepared_status: PreparedStatus) -> bool:
    """Whether any selected revision could possibly need rebasing.

    Cleanup rebase moves surviving descendants past merged ancestors, so a
    stack where no revision carries review identity cannot have any known
    merged PRs and has nothing for cleanup rebase to plan. Skipping the GitHub
    inspection here also avoids misreporting GitHub outages as rebase-blocking
    when there would have been nothing to rebase regardless.
    """

    for prepared_revision in prepared_status.prepared.status_revisions:
        cached = prepared_revision.cached_change
        if cached is not None and cached.has_review_identity:
            return True
    return False


def _stream_rebase(
    *,
    on_action: Callable[[CleanupAction], None] | None = None,
    prepared_rebase: PreparedRebase,
) -> RebaseResult:
    """Inspect and optionally execute a local rebase plan after merged changes."""

    prepared_status = prepared_rebase.prepared_status
    if not _prepared_rebase_has_potential_work(prepared_status=prepared_status):
        return RebaseResult(actions=(), blocked=False)
    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            inspect_stack_comments=True,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    prepared_status = prepared_rebase.prepared_status
    prepared = prepared_status.prepared
    path_revisions = _resolve_rebase_path_revisions(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    actions: list[CleanupAction] = []

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    if status_result.github_error is not None or status_result.github_repository is None:
        record_action(
            CleanupAction(
                kind="rebase",
                status="blocked",
                body=(
                    "cannot compute a rebase plan without live GitHub pull request "
                    "state; fix GitHub access and retry"
                ),
            )
        )
        return RebaseResult(
            actions=tuple(actions),
            blocked=True,
        )

    operation_plan = _plan_rebase_operations(
        path_revisions=path_revisions,
        prepared_status=prepared_status,
    )
    blocked = operation_plan.blocked
    merged_revisions = operation_plan.merged_revisions
    if not merged_revisions:
        return RebaseResult(
            actions=(),
            blocked=False,
        )

    closed_unmerged_revisions = operation_plan.closed_unmerged_revisions
    for action in operation_plan.pre_actions:
        record_action(action)
    rebase_plans = list(operation_plan.rebase_plans)

    rebase_intent_state = _start_rebase_intent(
        blocked=blocked,
        prepared=prepared,
        prepared_rebase=prepared_rebase,
        selected_revset=status_result.selected_revset,
    )

    client = prepared.client
    _rebase_succeeded = False
    try:
        _run_rebase_pass(
            blocked=blocked,
            client=client,
            closed_unmerged_revisions=closed_unmerged_revisions,
            prepared_rebase=prepared_rebase,
            rebase_plans=tuple(rebase_plans),
            record_action=record_action,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )

        _record_rebase_policy_actions(
            prefix=prepared_rebase.config.bookmark_prefix,
            merged_revisions=merged_revisions,
            record_action=record_action,
        )

        if not actions and merged_revisions:
            record_action(
                CleanupAction(
                    kind="rebase",
                    status="planned" if prepared_rebase.dry_run else "applied",
                    body=t"merged changes remain on the selected stack "
                    t"({ui.join(_revision_label_template, merged_revisions)}), but no "
                    t"surviving descendants need to move",
                )
            )

        _rebase_succeeded = True
        return RebaseResult(
            actions=tuple(actions),
            blocked=blocked,
        )
    finally:
        if (
            _rebase_succeeded
            and rebase_intent_state.intent_path is not None
            and rebase_intent_state.intent is not None
        ):
            retire_superseded_intents(
                rebase_intent_state.stale_intents,
                rebase_intent_state.intent,
            )
            rebase_intent_state.intent_path.unlink(missing_ok=True)


def _start_rebase_intent(
    *,
    blocked: bool,
    prepared,
    prepared_rebase: PreparedRebase,
    selected_revset: str,
) -> _RebaseIntentState:
    """Write a rebase intent before live rebases begin."""

    if blocked or prepared_rebase.dry_run:
        return _RebaseIntentState(intent=None, intent_path=None, stale_intents=[])

    ordered_change_ids = tuple(
        str(prepared_revision.revision.change_id)
        for prepared_revision in prepared.status_revisions
    )
    ordered_commit_ids = tuple(
        str(prepared_revision.revision.commit_id)
        for prepared_revision in prepared.status_revisions
    )
    intent = CleanupRebaseIntent(
        kind="cleanup-rebase",
        pid=os.getpid(),
        label=(
            f"cleanup --rebase for {short_change_id(ordered_change_ids[-1])} "
            f"(from {selected_revset})"
        ),
        display_revset=selected_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        started_at=datetime.now(UTC).isoformat(),
    )
    state_dir = prepared.state_store.require_writable()
    stale_intents = check_same_kind_intent(state_dir, intent)
    for loaded in stale_intents:
        if not isinstance(loaded.intent, CleanupRebaseIntent):
            continue
        match = match_cleanup_rebase_intent(
            intent=loaded.intent,
            current_change_ids=ordered_change_ids,
            current_commit_ids=ordered_commit_ids,
        )
        description = describe_intent(loaded.intent)
        if match == "exact":
            console.note(t"Continuing interrupted {description}")
        elif match == "same-logical":
            console.note(
                t"Note: interrupted {description} targeted the same logical stack, "
                t"but it has been rewritten. This {ui.cmd('cleanup --rebase')} run "
                t"will use the current stack."
            )
        elif match == "covered":
            console.note(
                t"Note: interrupted {description} targeted changes that are all "
                t"included in the current stack. This {ui.cmd('cleanup --rebase')} "
                t"run will use the current stack."
            )
        elif match == "trimmed":
            console.note(
                t"Note: interrupted {description} still includes changes that are no "
                t"longer on the current stack. This {ui.cmd('cleanup --rebase')} run "
                t"will use the current stack."
            )
        elif match == "overlap":
            console.warning(t"Warning: this rebase overlaps an incomplete earlier "
                            t"operation ({description})")
        else:
            console.note(t"Note: incomplete operation outstanding: {description}")
    return _RebaseIntentState(
        intent=intent,
        intent_path=write_new_intent(state_dir, intent),
        stale_intents=stale_intents,
    )


def _record_rebase_policy_actions(
    *,
    prefix: str,
    merged_revisions: tuple[ReviewStatusRevision, ...],
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Warn when a merged PR targeted another review branch."""

    for revision in merged_revisions:
        pull_request_number = revision.pull_request_number()
        if pull_request_number is None:
            continue
        base_ref = revision.pull_request_base_ref()
        if base_ref is None or not is_review_bookmark(base_ref, prefix=prefix):
            continue
        record_action(
            CleanupAction(
                kind="policy",
                status="planned",
                body=(
                    t"PR #{pull_request_number} merged into branch {ui.bookmark(base_ref)}; "
                    t"configure GitHub to block merges of PRs targeting "
                    t"{ui.bookmark(bookmark_glob(prefix))}"
                ),
            )
        )

def _resolve_rebase_path_revisions(
    *,
    prepared_status: PreparedStatus,
    status_result,
) -> tuple[ReviewStatusRevision, ...]:
    revisions_by_change_id = {
        revision.change_id: revision for revision in status_result.revisions
    }
    return tuple(
        revisions_by_change_id[prepared_revision.revision.change_id]
        for prepared_revision in prepared_status.prepared.status_revisions
        if prepared_revision.revision.change_id in revisions_by_change_id
    )


def _run_rebase_pass(
    *,
    blocked: bool,
    client: JjClient,
    closed_unmerged_revisions: tuple[ReviewStatusRevision, ...],
    prepared_rebase: PreparedRebase,
    rebase_plans: tuple[tuple[str, str | None], ...],
    record_action: Callable[[CleanupAction], None],
    trunk_commit_id: str,
) -> None:
    if not prepared_rebase.dry_run and not blocked:
        for source_change_id, destination_change_id in rebase_plans:
            source_revision = client.resolve_revision(source_change_id)
            destination_commit_id = _rebase_destination_commit_id(
                client=client,
                destination_change_id=destination_change_id,
                trunk_commit_id=trunk_commit_id,
            )
            if source_revision.only_parent_commit_id() == destination_commit_id:
                continue
            client.rebase_revision(
                source=source_change_id,
                destination=destination_commit_id,
            )
            record_action(
                CleanupAction(
                    kind="rebase",
                    status="applied",
                    body=(
                        t"rebase {ui.change_id(source_change_id)} onto "
                        t"{_rebase_destination_template(destination_change_id)}"
                    ),
                )
            )
        return

    for source_change_id, destination_change_id in rebase_plans:
        status = "blocked" if blocked else "planned"
        body = (
            t"rebase {ui.change_id(source_change_id)} onto "
            t"{_rebase_destination_template(destination_change_id)}"
        )
        if blocked and closed_unmerged_revisions:
            body = t"{body} once blocked changes on the stack are resolved"
        record_action(
            CleanupAction(
                kind="rebase",
                status=status,
                body=body,
            )
        )


def _rebase_destination_commit_id(
    *,
    client: JjClient,
    destination_change_id: str | None,
    trunk_commit_id: str,
) -> str:
    if destination_change_id is None:
        return trunk_commit_id
    return client.resolve_revision(destination_change_id).commit_id


def _plan_rebase_operations(
    *,
    path_revisions: tuple[ReviewStatusRevision, ...],
    prepared_status: PreparedStatus,
) -> _RebaseOperationPlan:
    merged_revisions = tuple(
        revision
        for revision in path_revisions
        if classify_review_status_revision(revision).pr_lifecycle == "merged"
    )
    closed_unmerged_revisions = tuple(
        revision
        for revision in path_revisions
        if classify_review_status_revision(revision).pr_lifecycle == "closed"
    )
    revisions_by_change_id = {revision.change_id: revision for revision in path_revisions}
    current_commit_id_by_change_id = {
        prepared_revision.revision.change_id: prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    }

    blocked, actions = _collect_rebase_pre_actions(
        closed_unmerged_revisions=closed_unmerged_revisions,
        current_commit_id_by_change_id=current_commit_id_by_change_id,
        merged_revisions=merged_revisions,
    )
    blocked, rebase_plans = _plan_rebase_rebases(
        actions=actions,
        blocked=blocked,
        prepared_status=prepared_status,
        revisions_by_change_id=revisions_by_change_id,
    )

    return _RebaseOperationPlan(
        blocked=blocked,
        closed_unmerged_revisions=closed_unmerged_revisions,
        merged_revisions=merged_revisions,
        pre_actions=tuple(actions),
        rebase_plans=tuple(rebase_plans),
    )


def _collect_rebase_pre_actions(
    *,
    closed_unmerged_revisions: tuple[ReviewStatusRevision, ...],
    current_commit_id_by_change_id: dict[str, str],
    merged_revisions: tuple[ReviewStatusRevision, ...],
) -> tuple[bool, list[CleanupAction]]:
    """Record blocking rebase conditions before survivor planning begins."""

    blocked = False
    actions: list[CleanupAction] = []
    for revision in closed_unmerged_revisions:
        blocked = True
        actions.append(
            CleanupAction(
                    kind="rebase",
                    status="blocked",
                    body=(
                        t"cannot rebase past {_revision_label_template(revision)} because "
                        t"PR #{revision.pull_request_number()} is closed without "
                        t"merge; decide whether to keep or drop that change first"
                    ),
                )
        )

    for revision in merged_revisions:
        cached_change = revision.cached_change
        if cached_change is None or cached_change.last_submitted_commit_id is None:
            continue
        current_commit_id = current_commit_id_by_change_id[revision.change_id]
        if current_commit_id == cached_change.last_submitted_commit_id:
            continue
        blocked = True
        actions.append(
            CleanupAction(
                kind="rebase",
                status="blocked",
                body=(
                    t"cannot rebase past {_revision_label_template(revision)} because it "
                    t"has local edits since last submit; push a new version first or "
                    t"rebase manually"
                ),
            )
        )

    return blocked, actions


def _plan_rebase_rebases(
    *,
    actions: list[CleanupAction],
    blocked: bool,
    prepared_status: PreparedStatus,
    revisions_by_change_id: dict[str, ReviewStatusRevision],
) -> tuple[bool, list[tuple[str, str | None]]]:
    """Plan survivor rebases after merged ancestors are removed from the path."""

    survivor_change_ids: list[str] = []
    rebase_plans: list[tuple[str, str | None]] = []
    for prepared_revision in prepared_status.prepared.status_revisions:
        revision = revisions_by_change_id.get(prepared_revision.revision.change_id)
        if revision is None:
            continue
        change_status = classify_review_status_revision(revision)
        if change_status.pr_lifecycle == "merged":
            continue
        if change_status.pr_lifecycle == "closed":
            continue
        if change_status.local == "divergent":
            blocked = True
            actions.append(
                CleanupAction(
                    kind="rebase",
                    status="blocked",
                    body=(
                        t"cannot rebase {_revision_label_template(revision)} while "
                        t"multiple visible revisions still share that change ID"
                    ),
                )
            )
            survivor_change_ids.append(revision.change_id)
            continue

        desired_parent_change_id = survivor_change_ids[-1] if survivor_change_ids else None
        if _rebase_parent_is_merged(
            parent_commit_id=prepared_revision.revision.only_parent_commit_id(),
            prepared_status=prepared_status,
            revisions_by_change_id=revisions_by_change_id,
        ):
            rebase_plans.append((revision.change_id, desired_parent_change_id))
        survivor_change_ids.append(revision.change_id)
    return blocked, rebase_plans


def _rebase_parent_is_merged(
    *,
    parent_commit_id: str | None,
    prepared_status: PreparedStatus,
    revisions_by_change_id: dict[str, ReviewStatusRevision],
) -> bool:
    for candidate in prepared_status.prepared.status_revisions:
        if candidate.revision.commit_id != parent_commit_id:
            continue
        revision = revisions_by_change_id.get(candidate.revision.change_id)
        return (
            revision is not None
            and classify_review_status_revision(revision).pr_lifecycle == "merged"
        )
    return False


async def _run_cleanup_async(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None] | None = None,
) -> CleanupResult:
    next_changes = dict(prepared_cleanup.state.changes)
    actions: list[CleanupAction] = []
    dry_run = prepared_cleanup.dry_run

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    # Write an intent file before the first mutation on live runs only.
    intent_path: Path | None = None
    _cleanup_succeeded = False
    stale_intents: list[LoadedIntent] = []
    if not dry_run:
        state_dir = prepared_cleanup.state_store.require_writable()
        _intent = CleanupIntent(
            kind="cleanup",
            pid=os.getpid(),
            label="cleanup",
            started_at=datetime.now(UTC).isoformat(),
        )
        stale_intents = check_same_kind_intent(state_dir, _intent)
        for _loaded in stale_intents:
            console.note(f"Note: a previous cleanup was interrupted ({_loaded.intent.label})")
        intent_path = write_new_intent(state_dir, _intent)

    try:
        if stale_reasons is None:
            stale_reasons = _stale_change_reasons(
                change_ids=tuple(prepared_cleanup.state.changes),
                jj_client=prepared_cleanup.jj_client,
            )
        if _cleanup_needs_remote_context(
            prepared_cleanup=prepared_cleanup,
            stale_reasons=stale_reasons,
        ):
            prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        prepared_changes = _run_local_cleanup_pass(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
            stale_reasons=stale_reasons,
        )
        if prepared_cleanup.github_repository is None:
            _save_cleanup_state_if_changed(
                next_changes=next_changes,
                prepared_cleanup=prepared_cleanup,
            )

            _cleanup_succeeded = True
            return CleanupResult(
                actions=tuple(actions),
            )

        if not any(
            prepared_change.inspect_stack_comment for prepared_change in prepared_changes
        ):
            _save_cleanup_state_if_changed(
                next_changes=next_changes,
                prepared_cleanup=prepared_cleanup,
            )

            _cleanup_succeeded = True
            return CleanupResult(
                actions=tuple(actions),
            )

        github_repository = prepared_cleanup.github_repository
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            await _run_stack_comment_cleanup_pass(
                github_client=github_client,
                github_repository=github_repository,
                next_changes=next_changes,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                record_action=record_action,
            )

        _save_cleanup_state_if_changed(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
        )

        _cleanup_succeeded = True
        return CleanupResult(
            actions=tuple(actions),
        )
    finally:
        if _cleanup_succeeded and intent_path is not None:
            for loaded in stale_intents:
                loaded.path.unlink(missing_ok=True)
            intent_path.unlink(missing_ok=True)

def _run_local_cleanup_pass(
    *,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    stale_reasons: dict[str, str | None],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    mutation_plans: list[_StaleCleanupMutationPlan] = []
    orphan_local_bookmark_plans: list[OrphanLocalBookmarkCleanupPlan] = []
    for change_id, cached_change in prepared_cleanup.state.changes.items():
        stale_reason = stale_reasons.get(change_id)
        bookmark_state = prepared_cleanup.bookmark_states.get(
            cached_change.bookmark or "",
            BookmarkState(name=cached_change.bookmark or ""),
        )
        prepared_change = PreparedCleanupChange(
            bookmark_state=bookmark_state,
            cached_change=cached_change,
            change_id=change_id,
            inspect_stack_comment=_should_inspect_stack_comment_cleanup(
                bookmark_state=bookmark_state,
                cached_change=cached_change,
                remote=prepared_cleanup.remote,
                stale_reason=stale_reason,
            ),
            stale_reason=stale_reason,
        )
        prepared_changes.append(prepared_change)
        mutation_plan = _process_stale_cleanup_change(
            next_changes=next_changes,
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        if mutation_plan is not None:
            mutation_plans.append(mutation_plan)

    tracked_bookmarks = {
        cached_change.bookmark
        for cached_change in prepared_cleanup.state.changes.values()
        if cached_change.bookmark is not None
    }
    for bookmark, bookmark_state in sorted(prepared_cleanup.bookmark_states.items()):
        if bookmark in tracked_bookmarks or not is_review_bookmark(
            bookmark,
            prefix=prepared_cleanup.config.bookmark_prefix,
        ):
            continue
        orphan_plan = _plan_orphan_local_bookmark_cleanup(
            prefix=prepared_cleanup.config.bookmark_prefix,
            bookmark_state=bookmark_state,
            jj_client=prepared_cleanup.jj_client,
        )
        if orphan_plan is None:
            continue
        if prepared_cleanup.dry_run:
            record_action(orphan_plan.action)
            continue
        if orphan_plan.action.status != "planned":
            record_action(orphan_plan.action)
            continue
        orphan_local_bookmark_plans.append(orphan_plan)

    if not prepared_cleanup.dry_run:
        _apply_stale_cleanup_mutation_plans(
            jj_client=prepared_cleanup.jj_client,
            mutation_plans=tuple(mutation_plans),
            orphan_local_bookmark_plans=tuple(orphan_local_bookmark_plans),
            record_action=record_action,
            remote=prepared_cleanup.remote,
        )
        _save_cleanup_state_if_changed(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
        )
    return tuple(prepared_changes)


def _process_stale_cleanup_change(
    *,
    next_changes: dict[str, CachedChange],
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> _StaleCleanupMutationPlan | None:
    stale_reason = prepared_change.stale_reason
    if stale_reason is None:
        return None
    if is_open_pr_record(prepared_change.cached_change):
        pull_request_number = prepared_change.cached_change.pr_number
        assert pull_request_number is not None
        close_hint = ui.cmd(
            f"jj-review close --cleanup --pull-request {pull_request_number}"
        )
        body = (
            t"preserve open orphan {ui.change_id(prepared_change.change_id)} "
            t"(run {close_hint} to retire it)"
        )
        record_action(
            CleanupAction(
                kind="tracking",
                status="skipped",
                body=body,
            )
        )
        return None

    record_action(
        CleanupAction(
            kind="tracking",
            status="planned" if prepared_cleanup.dry_run else "applied",
            body=t"remove tracking for {ui.change_id(prepared_change.change_id)} "
            t"({stale_reason})",
        )
    )
    if not prepared_cleanup.dry_run:
        next_changes.pop(prepared_change.change_id, None)

    local_bookmark_plan = _plan_local_bookmark_cleanup(
        cleanup_user_bookmarks=prepared_cleanup.config.cleanup_user_bookmarks,
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.config.bookmark_prefix,
        cached_change=prepared_change.cached_change,
        stale_reason=stale_reason,
    )
    remote_plan = _plan_remote_branch_cleanup(
        cleanup_user_bookmarks=prepared_cleanup.config.cleanup_user_bookmarks,
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.config.bookmark_prefix,
        cached_change=prepared_change.cached_change,
        local_bookmark_forget_planned=(
            local_bookmark_plan is not None and local_bookmark_plan.status == "planned"
        ),
        remote=prepared_cleanup.remote,
    )
    if prepared_cleanup.dry_run:
        if local_bookmark_plan is not None:
            record_action(local_bookmark_plan)
        if remote_plan is not None:
            record_action(remote_plan.action)
        return None

    if local_bookmark_plan is not None and local_bookmark_plan.status != "planned":
        record_action(local_bookmark_plan)
    if remote_plan is not None and remote_plan.action.status != "planned":
        record_action(remote_plan.action)

    if (local_bookmark_plan is None or local_bookmark_plan.status != "planned") and (
        remote_plan is None or remote_plan.action.status != "planned"
    ):
        return None

    return _StaleCleanupMutationPlan(
        cached_change=prepared_change.cached_change,
        local_bookmark_action=local_bookmark_plan,
        remote_plan=remote_plan,
    )


def _apply_stale_cleanup_mutation_plans(
    *,
    jj_client: JjClient,
    mutation_plans: tuple[_StaleCleanupMutationPlan, ...],
    orphan_local_bookmark_plans: tuple[OrphanLocalBookmarkCleanupPlan, ...] = (),
    record_action: Callable[[CleanupAction], None],
    remote: GitRemote | None,
) -> None:
    remote_deletions: list[tuple[str, str]] = []
    remote_actions: list[CleanupAction] = []
    local_bookmarks: list[str] = []
    local_actions: list[CleanupAction] = []

    for mutation_plan in mutation_plans:
        remote_plan = mutation_plan.remote_plan
        if (
            remote_plan is not None
            and remote_plan.action.status == "planned"
            and remote is not None
            and remote_plan.expected_remote_target is not None
        ):
            bookmark = mutation_plan.cached_change.bookmark
            if bookmark is None:
                raise AssertionError("Planned remote branch cleanup requires a bookmark.")
            remote_deletions.append((bookmark, remote_plan.expected_remote_target))
            remote_actions.append(remote_plan.action)

        local_bookmark_action = mutation_plan.local_bookmark_action
        if local_bookmark_action is not None and local_bookmark_action.status == "planned":
            bookmark = mutation_plan.cached_change.bookmark
            if bookmark is None:
                raise AssertionError("Planned local bookmark cleanup requires a bookmark.")
            local_bookmarks.append(bookmark)
            local_actions.append(local_bookmark_action)

    for orphan_plan in orphan_local_bookmark_plans:
        if orphan_plan.action.status != "planned":
            continue
        local_bookmarks.append(orphan_plan.bookmark)
        local_actions.append(orphan_plan.action)

    remote_deleted = False
    try:
        if remote_deletions and remote is not None:
            jj_client.delete_remote_bookmarks(
                remote=remote.name,
                deletions=tuple(remote_deletions),
                fetch=False,
            )
            remote_deleted = True
        if local_bookmarks:
            jj_client.forget_bookmarks(tuple(local_bookmarks))
    finally:
        if remote_deleted and remote is not None:
            jj_client.fetch_remote(remote=remote.name)

    for remote_action in remote_actions:
        record_action(replace(remote_action, status="applied"))
    for local_action in local_actions:
        record_action(replace(local_action, status="applied"))


def _save_cleanup_state_if_changed(
    *,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
) -> None:
    if not prepared_cleanup.dry_run and next_changes != prepared_cleanup.state.changes:
        prepared_cleanup.state_store.save(
            prepared_cleanup.state.model_copy(update={"changes": dict(next_changes)})
        )


async def _run_stack_comment_cleanup_pass(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    next_changes: dict[str, CachedChange],
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    stack_comment_changes = tuple(
        prepared_change
        for prepared_change in prepared_changes
        if prepared_change.inspect_stack_comment
    )
    with console.progress(
        description="Inspecting stack comments",
        total=len(stack_comment_changes),
    ) as progress:
        comment_plans = await run_bounded_tasks(
            concurrency=_GITHUB_INSPECTION_CONCURRENCY,
            items=stack_comment_changes,
            run_item=lambda prepared_change: _plan_stack_comment_cleanup(
                cached_change=prepared_change.cached_change,
                bookmark_state=prepared_change.bookmark_state,
                github_client=github_client,
                github_repository=github_repository,
            ),
            on_success=lambda _index, _result: progress.advance(),
        )
    for prepared_change, comment_plan in zip(
        stack_comment_changes,
        comment_plans,
        strict=True,
    ):
        if comment_plan is None:
            continue
        await _apply_stack_comment_cleanup_action(
            comment_plan=comment_plan,
            change_id=prepared_change.change_id,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )


async def _apply_stack_comment_cleanup_action(
    *,
    comment_plan: StackCommentCleanupPlan,
    change_id: str,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    applied_comments = False
    for action, (comment_id, kind) in zip(
        comment_plan.actions,
        comment_plan.comments,
        strict=True,
    ):
        comment_action = action
        if not prepared_cleanup.dry_run and comment_action.status == "planned":
            try:
                await github_client.delete_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=comment_id,
                )
            except GithubClientError as error:
                raise CliError(
                    f"Could not delete {stack_comment_label(kind)} #{comment_id}"
                ) from error
            applied_comments = True
            comment_action = replace(action, status="applied")
        record_action(comment_action)
    for action in comment_plan.actions[len(comment_plan.comments) :]:
        record_action(action)
    if applied_comments and change_id in next_changes:
        next_changes[change_id] = next_changes[change_id].model_copy(
            update={
                "navigation_comment_id": None,
                "overview_comment_id": None,
            }
        )
    if not prepared_cleanup.dry_run:
        prepared_cleanup.state_store.save(
            prepared_cleanup.state.model_copy(update={"changes": dict(next_changes)})
        )


def _resolve_remote(*, jj_client: JjClient) -> tuple[GitRemote | None, ErrorMessage | None]:
    try:
        return select_submit_remote(jj_client.list_git_remotes()), None
    except CliError as error:
        return None, error_message(error)


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.remote_context_loaded:
        return prepared_cleanup

    remote, remote_error = _resolve_remote(jj_client=prepared_cleanup.jj_client)
    github_repository = None
    github_error = None
    if remote is not None:
        github_repository = parse_github_repo(remote)
        if github_repository is None:
            github_error = (
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(remote.name)}. Use a GitHub remote URL."
            )

    return replace(
        prepared_cleanup,
        github_repository=github_repository,
        github_repository_error=github_error,
        remote=remote,
        remote_error=remote_error,
        remote_context_loaded=True,
    )


def _cleanup_needs_remote_context(
    *,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None],
) -> bool:
    """Whether plain cleanup might need remote or GitHub state beyond local checks."""

    for change_id, cached_change in prepared_cleanup.state.changes.items():
        stale_reason = stale_reasons.get(change_id)
        bookmark = cached_change.bookmark
        bookmark_state = prepared_cleanup.bookmark_states.get(
            bookmark or "",
            BookmarkState(name=bookmark or ""),
        )
        if (
            stale_reason is not None
            and bookmark is not None
            and bookmark_state.remote_targets
            and (
                is_review_bookmark(
                    bookmark,
                    prefix=prepared_cleanup.config.bookmark_prefix,
                )
                or prepared_cleanup.config.cleanup_user_bookmarks
            )
        ):
            return True
        if _stack_comment_cleanup_eligibility(
            cached_change=cached_change,
            stale_reason=stale_reason,
        ) != "skip":
            return True
    return False


def _stack_comment_cleanup_eligibility(
    *,
    cached_change: CachedChange,
    stale_reason: str | None,
) -> StackCommentCleanupEligibility:
    """Classify whether cleanup can inspect stack comments for this change."""

    if cached_change.pr_number is None:
        if cached_change.is_unlinked and cached_change.bookmark is not None:
            return "inspect"
        return "skip"
    if cached_change.is_unlinked:
        return "inspect"
    if (
        cached_change.bookmark is None
        and cached_change.navigation_comment_id is None
        and cached_change.overview_comment_id is None
    ):
        return "skip"
    if stale_reason is None:
        return "inspect"
    if cached_change.pr_state in {"closed", "merged"}:
        return "skip"
    if (
        cached_change.navigation_comment_id is not None
        or cached_change.overview_comment_id is not None
    ):
        return "inspect"
    if cached_change.bookmark is None:
        return "skip"
    return "needs-remote-check"


def _load_bookmark_states(
    *,
    prefix: str,
    jj_client: JjClient,
    state: ReviewState,
) -> dict[str, BookmarkState]:
    bookmark_states = jj_client.list_bookmark_states()
    tracked_bookmarks = {
        cached_change.bookmark
        for cached_change in state.changes.values()
        if cached_change.bookmark is not None
    }
    relevant_bookmarks = {
        bookmark
        for bookmark, bookmark_state in bookmark_states.items()
        if is_review_bookmark(bookmark, prefix=prefix)
        and bookmark_state.local_targets
    }
    relevant_bookmarks.update(tracked_bookmarks)

    if not relevant_bookmarks:
        return {}

    filtered = {
        bookmark: bookmark_states[bookmark]
        for bookmark in relevant_bookmarks
        if bookmark in bookmark_states
    }
    for bookmark in tracked_bookmarks:
        filtered.setdefault(bookmark, BookmarkState(name=bookmark))
    return filtered


def _stale_change_reasons(
    *,
    change_ids: tuple[str, ...],
    jj_client: JjClient,
) -> dict[str, str | None]:
    matched_revisions = jj_client.query_revisions_by_change_ids(change_ids)
    reasons: dict[str, str | None] = {}

    for change_id in change_ids:
        revisions = matched_revisions.get(change_id, ())
        if not revisions:
            reasons[change_id] = "no visible local change matches that cached change ID"
            continue
        if len(revisions) > 1:
            reasons[change_id] = "multiple visible revisions still share that change ID"
            continue

        revision = revisions[0]
        if not revision.is_reviewable():
            reasons[change_id] = "local change is no longer reviewable"
            continue

        reasons[change_id] = None

    candidate_revisions = tuple(
        revisions[0]
        for change_id in change_ids
        if reasons.get(change_id) is None
        for revisions in (matched_revisions.get(change_id, ()),)
        if revisions
    )
    supported_change_ids = jj_client.supported_review_stack_change_ids(candidate_revisions)
    for revision in candidate_revisions:
        if revision.change_id not in supported_change_ids:
            reasons[revision.change_id] = (
                "local change no longer participates in a supported review stack"
            )
    return reasons


def _plan_remote_branch_cleanup(
    *,
    cleanup_user_bookmarks: bool,
    bookmark_state: BookmarkState,
    prefix: str,
    cached_change: CachedChange,
    local_bookmark_forget_planned: bool,
    remote: GitRemote | None,
) -> RemoteBranchCleanupPlan | None:
    bookmark = cached_change.bookmark
    if bookmark is None:
        return None
    if cached_change.pr_number is None:
        return None
    if cached_change.manages_bookmark:
        if not is_review_bookmark(bookmark, prefix=prefix):
            return None
    elif not cleanup_user_bookmarks:
        return None
    if remote is None:
        return None

    remote_state = bookmark_state.remote_target(remote.name)
    if remote_state is None or not remote_state.targets:
        return None

    branch_label = f"{bookmark}@{remote.name}"
    if bookmark_state.local_targets and not local_bookmark_forget_planned:
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                status="blocked",
                body=(
                    t"cannot delete {ui.bookmark(branch_label)} while the local "
                    t"bookmark {ui.bookmark(bookmark)} still exists"
                ),
            ),
        )
    if len(remote_state.targets) > 1:
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                status="blocked",
                body=(
                    t"cannot delete {ui.bookmark(branch_label)} because the remote "
                    t"bookmark is conflicted"
                ),
            ),
        )

    return RemoteBranchCleanupPlan(
        action=CleanupAction(
            kind="remote branch",
            status="planned",
            body=t"delete {ui.bookmark(branch_label)}",
        ),
        expected_remote_target=remote_state.target,
    )


def _plan_local_bookmark_cleanup(
    *,
    cleanup_user_bookmarks: bool,
    bookmark_state: BookmarkState,
    prefix: str,
    cached_change: CachedChange,
    stale_reason: str,
) -> CleanupAction | None:
    bookmark = cached_change.bookmark
    if bookmark is None:
        return None
    if cached_change.manages_bookmark:
        if not is_review_bookmark(bookmark, prefix=prefix):
            return None
    elif not cleanup_user_bookmarks:
        return None
    if not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return CleanupAction(
            kind="local bookmark",
            status="blocked",
            body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
        )

    local_target = bookmark_state.local_target
    if local_target is None:
        return None

    expected_target = cached_change.last_submitted_commit_id
    if expected_target is not None and local_target != expected_target:
        return CleanupAction(
            kind="local bookmark",
            status="blocked",
            body=(
                t"cannot forget {ui.bookmark(bookmark)} because it already points "
                t"to a different revision"
            ),
        )

    return CleanupAction(
        kind="local bookmark",
        status="planned",
        body=t"forget {ui.bookmark(bookmark)} ({stale_reason})",
    )


def _plan_orphan_local_bookmark_cleanup(
    *,
    prefix: str,
    bookmark_state: BookmarkState,
    jj_client: JjClient,
) -> OrphanLocalBookmarkCleanupPlan | None:
    bookmark = bookmark_state.name
    if not is_review_bookmark(bookmark, prefix=prefix) or not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return OrphanLocalBookmarkCleanupPlan(
            bookmark=bookmark,
            action=CleanupAction(
                kind="local bookmark",
                status="blocked",
                body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
            ),
        )

    local_target = bookmark_state.local_target
    if local_target is None:
        return None

    revisions = jj_client.query_revisions(local_target)
    if not revisions:
        stale_reason = "target is no longer visible locally"
    else:
        revision = revisions[0]
        if not revision.is_reviewable():
            stale_reason = "target is no longer reviewable"
        elif revision.change_id not in jj_client.supported_review_stack_change_ids((revision,)):
            stale_reason = "target no longer participates in a supported review stack"
        else:
            return None

    return OrphanLocalBookmarkCleanupPlan(
        bookmark=bookmark,
        action=CleanupAction(
            kind="local bookmark",
            status="planned",
            body=t"forget {ui.bookmark(bookmark)} ({stale_reason})",
        ),
    )


def _should_inspect_stack_comment_cleanup(
    *,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    remote: GitRemote | None,
    stale_reason: str | None,
) -> bool:
    eligibility = _stack_comment_cleanup_eligibility(
        cached_change=cached_change,
        stale_reason=stale_reason,
    )
    if eligibility == "inspect":
        return True
    if eligibility == "skip":
        return False
    if remote is None:
        return False

    remote_state = bookmark_state.remote_target(remote.name)
    return remote_state is None or not remote_state.targets


async def _plan_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
    github_repository,
) -> StackCommentCleanupPlan | None:
    pull_request_number = cached_change.pr_number
    if pull_request_number is None and cached_change.is_unlinked:
        pull_request_number = await _resolve_unlinked_pull_request_number(
            bookmark_state=bookmark_state,
            github_client=github_client,
            github_repository=github_repository,
        )
        if isinstance(pull_request_number, CleanupAction):
            return StackCommentCleanupPlan(actions=(pull_request_number,))

    if pull_request_number is None:
        return None

    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CliError(f"Could not load pull request #{pull_request_number}") from error

    if not cached_change.is_unlinked:
        bookmark = cached_change.bookmark
        if bookmark is None:
            return None
        expected_label = f"{github_repository.owner}:{bookmark}"
        if pull_request.head.ref == bookmark and pull_request.head.label == expected_label:
            return None

    managed_comments = await _resolve_managed_comments(
        cached_change=cached_change,
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=pull_request_number,
    )
    if isinstance(managed_comments, CleanupAction):
        return StackCommentCleanupPlan(actions=(managed_comments,))
    if not managed_comments:
        return None

    return StackCommentCleanupPlan(
        actions=tuple(
            CleanupAction(
                kind=stack_comment_label(kind),
                status="planned",
                body=(
                    f"delete {stack_comment_label(kind)} #{comment.id} from PR "
                    f"#{pull_request_number}"
                ),
            )
            for kind, comment in managed_comments
        ),
        comments=tuple((comment.id, kind) for kind, comment in managed_comments),
    )



async def _resolve_managed_comments(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> tuple[tuple[StackCommentKind, GithubIssueComment], ...] | CleanupAction:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not list stack comments for pull request #{pull_request_number}"
        ) from error

    resolved: list[tuple[StackCommentKind, GithubIssueComment]] = []
    for kind, cached_comment_id in (
        ("navigation", cached_change.navigation_comment_id),
        ("overview", cached_change.overview_comment_id),
    ):
        cached_comment = None
        if cached_comment_id is not None:
            cached_comment = next(
                (comment for comment in comments if comment.id == cached_comment_id),
                None,
            )
            if cached_comment is not None and not _comment_matches_kind(
                body=cached_comment.body,
                kind=kind,
            ):
                return CleanupAction(
                    kind=stack_comment_label(kind),
                    status="blocked",
                    body=(
                        f"cannot delete saved {stack_comment_label(kind)} "
                        f"#{cached_comment.id} because it does not belong to us"
                    ),
                )
        if cached_comment is not None:
            resolved.append((kind, cached_comment))
            continue

        matching_comments = [
            comment for comment in comments if _comment_matches_kind(body=comment.body, kind=kind)
        ]
        if len(matching_comments) > 1:
            return CleanupAction(
                kind=stack_comment_label(kind),
                status="blocked",
                body=(
                    f"cannot delete {stack_comment_label(kind)}s because GitHub reports "
                    f"multiple candidates on PR #{pull_request_number}"
                ),
            )
        if matching_comments:
            resolved.append((kind, matching_comments[0]))

    return tuple(resolved)


async def _resolve_unlinked_pull_request_number(
    *,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
    github_repository,
) -> int | CleanupAction | None:
    if bookmark_state.name == "":
        return None

    try:
        pull_requests = await github_client.list_pull_requests(
            github_repository.owner,
            github_repository.repo,
            head=f"{github_repository.owner}:{bookmark_state.name}",
            state="all",
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not list pull requests for unlinked bookmark "
            t"{ui.bookmark(bookmark_state.name)}"
        ) from error

    if not pull_requests:
        return None
    if len(pull_requests) > 1:
        return CleanupAction(
            kind="stack navigation comment",
            status="blocked",
            body=(
                t"cannot delete stack navigation comments because GitHub reports multiple "
                t"pull requests for unlinked bookmark {ui.bookmark(bookmark_state.name)}"
            ),
        )
    return pull_requests[0].number
