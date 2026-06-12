"""Shared cleanup command models, persistence, and rendering helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._close_actions import emit_action_row
from jj_stack.errors import ErrorMessage
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
    UnresolvedGithubTarget,
)
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.review_state import CachedChange, ReviewState
from jj_stack.review.change_status import ReviewChangeStatus
from jj_stack.review.status import PreparedStatus, ReviewStatusRevision
from jj_stack.state.journal import OperationJournal
from jj_stack.ui import Message, plain_text

CleanupActionStatus = Literal["applied", "blocked", "planned", "skipped"]
type CleanupBody = Message


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

    context: CommandContext
    bookmark_states: dict[str, BookmarkState]
    # None until plain cleanup proves it needs remote or GitHub state.
    github_target: GithubTarget | UnresolvedGithubTarget | None
    dry_run: bool
    state: ReviewState

    @property
    def remote(self) -> GitRemote | None:
        """The selected Git remote, once remote context is loaded and one resolved."""

        return self.github_target.remote if self.github_target is not None else None


@dataclass(slots=True)
class _CleanupSaver:
    """Persist cleanup state and emit saved_state_update events per disk write."""

    journal: OperationJournal
    last_persisted: dict[str, CachedChange]
    prepared_cleanup: PreparedCleanup

    def save_if_changed(self, next_changes: dict[str, CachedChange]) -> None:
        if self.prepared_cleanup.dry_run or next_changes == self.last_persisted:
            return
        self.journal.record_saved_state_updates(
            before=self.last_persisted,
            after=next_changes,
        )
        self.prepared_cleanup.context.state_store.save(
            self.prepared_cleanup.state.model_copy(update={"changes": dict(next_changes)})
        )
        self.last_persisted = dict(next_changes)


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
    remote_state: RemoteBookmarkState | None
    review_status: ReviewChangeStatus
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

    context: CommandContext
    dry_run: bool
    prepared_status: PreparedStatus


@dataclass(frozen=True, slots=True)
class _ClassifiedCleanupRebaseRevision:
    """A cleanup-rebase path revision with its derived review status."""

    revision: ReviewStatusRevision
    status: ReviewChangeStatus


@dataclass(frozen=True, slots=True)
class _RebaseOperationPlan:
    """Derived rebase planning data before preview/live rendering."""

    blocked: bool
    closed_unmerged_revisions: tuple[ReviewStatusRevision, ...]
    merged_revisions: tuple[ReviewStatusRevision, ...]
    pre_actions: tuple[CleanupAction, ...]
    rebase_plans: tuple[tuple[str, str | None], ...]


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
        github_repository=prepared_status.github_repository,
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


def _emit_severity_lines(lines: tuple[tuple[str, str], ...]) -> None:
    for severity, line in lines:
        if severity == "warning":
            console.warning(line)
        else:
            console.output(line)


def _emit_output_lines(lines: tuple[str, ...]) -> None:
    for line in lines:
        console.output(line)


def _build_action_streamer(
    *,
    header: str,
) -> Callable[[CleanupAction], None]:
    """Print the action header once, then stream actions as they arrive."""

    header_printed = False

    def emit_action(action: CleanupAction) -> None:
        nonlocal header_printed
        if not header_printed:
            console.output(header)
            header_printed = True
        emit_action_row(kind=action.kind, status=action.status, body=action.body)

    return emit_action


def _render_remote_and_github_lines(
    *,
    remote: GitRemote | None,
    remote_error: ErrorMessage | None,
    github_repository: GithubRepoAddress | None,
    github_error: ErrorMessage | None,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        ("warning", plain_text(message))
        for message in remote_and_github_unavailable_messages(
            github_error=github_error,
            github_repository=github_repository,
            remote=remote,
            remote_error=remote_error,
        )
    )


def _revision_label_template(revision: ReviewStatusRevision) -> ui.Message:
    return t"{revision.subject} ({ui.change_id(revision.change_id)})"


def _rebase_destination_template(destination_change_id: str | None) -> ui.Message:
    if destination_change_id is None:
        return ui.revset("trunk()")
    return ui.change_id(destination_change_id)
