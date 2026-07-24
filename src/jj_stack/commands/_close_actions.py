"""Shared types and rendering helpers for close command action rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import (
    StackCommentKind,
    comment_matches_kind,
    delete_stack_comment,
    stack_comment_label,
)
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_stack.models.github import GithubIssueComment, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline
from jj_stack.review.bookmarks import (
    bookmark_cleanup_allowed,
    classify_local_bookmark_forget,
    local_bookmark_forget_blocked_body,
)
from jj_stack.review.change_status import classify_review_change
from jj_stack.review.observation import (
    RepositoryObservation,
    observe_reviews,
)
from jj_stack.state.store import ReviewStateStore
from jj_stack.ui import Message, plain_text

ActionPresentationStatus = Literal["applied", "blocked", "planned", "skipped"]
CloseActionStatus = Literal["applied", "blocked", "planned"]
type CloseActionBody = Message


@dataclass(frozen=True, slots=True)
class BookmarkCleanupPlan:
    """Resolved bookmark cleanup actions for one cached change."""

    local_forget: bool
    remote_delete: bool
    blocked: bool = False


class BookmarkCleanupRun(Protocol):
    """Execution state needed to apply bookmark cleanup mutations."""

    @property
    def dry_run(self) -> bool: ...

    @property
    def jj_client(self) -> JjClient: ...


@dataclass(frozen=True, slots=True)
class CloseAction:
    """One close action that was planned, applied, or blocked."""

    kind: str
    status: CloseActionStatus
    body: CloseActionBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


def github_observation_blocker() -> CloseAction:
    """Return the shared user-facing blocker for unavailable GitHub facts."""

    return CloseAction(
        kind="close",
        body=(
            "cannot inspect pull requests tracked by jj-stack without live GitHub state; "
            "fix GitHub access and retry"
        ),
        status="blocked",
    )


def authorize_tracked_review(
    *,
    allowed_states: frozenset[str],
    change_id: str,
    observation: RepositoryObservation,
    preview_closed_dependents: frozenset[int] = frozenset(),
    require_no_open_dependents: bool = False,
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    submitted_baseline: SubmittedBaseline,
) -> tuple[GithubPullRequest | None, CloseAction | None]:
    """Authorize one exact unchanged review from shared policy-free facts."""

    observed = observation.reviews[change_id]
    pull_request_number = review_identity.pr_number
    pull_request = observed.pull_request
    kind = "close"
    reason: Message | None = None
    if (observed.identity, observed.baseline) != (review_identity, submitted_baseline):
        kind = "tracking"
        reason = (
            t"tracking for {ui.change_id(change_id)} changed during this operation; "
            t"reload and retry"
        )
    elif review_identity.repository_key != observation.repository.repository_key:
        reason = (
            t"cannot inspect saved PR #{pull_request_number} because it belongs to a "
            t"different GitHub repository"
        )
    elif change_id in observation.duplicate_claim_change_ids:
        reason = (
            t"cannot inspect saved PR #{pull_request_number} because multiple tracked changes "
            t"claim its PR number or branch"
        )
    elif pull_request is None:
        reason = t"PR #{pull_request_number} is no longer on GitHub"
    else:
        pull_request = pull_request.normalize_state()
    if reason is None:
        assert pull_request is not None
        exact_link = (
            review_identity.matches_pull_request(pull_request),
            tuple(pr.number for pr in observed.head_pull_requests),
        )
        if exact_link != (True, (pull_request_number,)):
            reason = (
                t"cannot inspect saved PR #{pull_request_number} because its live PR and head "
                t"no longer uniquely match {ui.bookmark(review_identity.head_ref)}"
            )
        elif pull_request.state not in allowed_states:
            reason = (
                t"cannot mutate saved PR #{pull_request_number} because GitHub now reports "
                t"state {pull_request.state!r}"
            )
    check_dependents = (reason is None, require_no_open_dependents) == (True, True)
    if check_dependents:
        open_pull_requests_by_base = observation.open_pull_requests_by_base
        assert open_pull_requests_by_base is not None
        observed_dependents = open_pull_requests_by_base.get(review_identity.head_ref, ())
        dependents = tuple(
            filter(
                lambda item: item.number not in preview_closed_dependents,
                observed_dependents,
            )
        )
        # A full 100-result page may hide another dependent, so it also fails closed.
        blockers = dependents[:1] or observed_dependents[99:100]
        if blockers:
            kind = "remote branch"
            dependent = blockers[0]
            reason = (
                t"preserve PR #{pull_request_number}'s branch and tracking because open "
                t"PR #{dependent.number} still uses {ui.bookmark(review_identity.head_ref)} "
                t"as its base; close or retarget PR #{dependent.number}, then rerun "
                t"{ui.cmd(retry_command)}"
            )
    return (
        pull_request,
        None if reason is None else CloseAction(kind=kind, body=reason, status="blocked"),
    )


async def authorize_current_tracked_pull_request(
    *,
    allowed_states: frozenset[str],
    change_id: str,
    github_client: GithubClient,
    require_no_open_dependents: bool = False,
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    state_store: ReviewStateStore,
    submitted_baseline: SubmittedBaseline,
) -> tuple[GithubPullRequest | None, CloseAction | None]:
    """Authorize one unchanged tracking pair with all saved claims in context."""

    try:
        observation = await observe_reviews(
            change_ids=(change_id,),
            github_client=github_client,
            include_open_dependents=require_no_open_dependents,
            state_store=state_store,
        )
    except GithubClientError:
        return None, github_observation_blocker()
    return authorize_tracked_review(
        allowed_states=allowed_states,
        change_id=change_id,
        observation=observation,
        require_no_open_dependents=require_no_open_dependents,
        retry_command=retry_command,
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )


async def close_current_tracked_pull_request(
    *,
    change_id: str,
    dry_run: bool,
    github_client: GithubClient,
    observed_pull_request: GithubPullRequest | None,
    review_identity: ReviewIdentity,
    state_store: ReviewStateStore,
    submitted_baseline: SubmittedBaseline,
    target_label: Message,
) -> tuple[GithubPullRequest | None, CloseAction | None]:
    """Close a freshly authorized open PR, or accept an already-ended PR."""

    if dry_run:
        pull_request = observed_pull_request
        blocker = None
    else:
        pull_request, blocker = await authorize_current_tracked_pull_request(
            allowed_states=frozenset({"open", "closed", "merged"}),
            change_id=change_id,
            github_client=github_client,
            review_identity=review_identity,
            state_store=state_store,
            submitted_baseline=submitted_baseline,
        )
    if blocker is not None:
        return pull_request, blocker
    if pull_request is None:
        raise AssertionError("Tracked close requires an exact pull request.")
    if pull_request.state in {"closed", "merged"}:
        return pull_request, None
    if pull_request.state != "open":
        raise AssertionError("Tracked close authorization returned an unexpected lifecycle.")
    if not dry_run:
        await github_client.close_pull_request(pull_number=pull_request.number)
    return (
        pull_request,
        CloseAction(
            kind="pull request",
            body=t"close PR #{pull_request.number} for {target_label}",
            status="planned" if dry_run else "applied",
        ),
    )


@dataclass(frozen=True, slots=True)
class ManagedCommentLookup:
    """One resolution result for a managed stack comment on a pull request.

    Exactly one of ``comment`` or ``blocked_reason`` is set; the other is
    ``None``. Kinds with neither a cached id nor a body-marker match are
    omitted from the result rather than represented as a third state.
    """

    kind: StackCommentKind
    comment: GithubIssueComment | None = None
    blocked_reason: str | None = None


async def find_managed_comments(
    *,
    github_client: GithubClient,
    pull_request_number: int,
) -> tuple[ManagedCommentLookup, ...]:
    """Discover managed stack comments for one PR via a single list call.

    Managed comments are identified by their body markers alone. Returns
    entries only for kinds that resolved to a delete target or were blocked;
    kinds with no body-marker match are omitted.
    """

    kinds: tuple[StackCommentKind, ...] = ("navigation", "overview")

    try:
        comments = await github_client.list_issue_comments(
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return ()
        reason = error.user_facing_reason()
        return tuple(
            ManagedCommentLookup(
                kind=kind,
                blocked_reason=(
                    f"cannot inspect {stack_comment_label(kind)}s for PR "
                    f"#{pull_request_number}: {reason}"
                ),
            )
            for kind in kinds
        )

    return tuple(
        entry
        for kind in kinds
        for entry in (
            _resolve_managed_comment_from_listed(
                comments=comments,
                kind=kind,
                pull_request_number=pull_request_number,
            ),
        )
        if entry is not None
    )


async def apply_managed_comment_cleanup(
    *,
    change_id: str,
    dry_run: bool,
    github_client: GithubClient,
    lookups: tuple[ManagedCommentLookup, ...],
    review_identity: ReviewIdentity,
    state_store: ReviewStateStore,
    submitted_baseline: SubmittedBaseline,
) -> tuple[tuple[CloseAction, ...], bool]:
    """Delete preflighted comments through one fresh marker and PR boundary."""

    actions: list[CloseAction] = []
    for lookup in lookups:
        comment = lookup.comment
        if comment is None:
            continue
        deleted = True
        if not dry_run:
            _pull_request, blocker = await authorize_current_tracked_pull_request(
                allowed_states=frozenset({"closed", "merged"}),
                change_id=change_id,
                github_client=github_client,
                review_identity=review_identity,
                state_store=state_store,
                submitted_baseline=submitted_baseline,
            )
            if blocker is not None:
                return ((*actions, blocker), False)
            try:
                deleted = await delete_stack_comment(
                    comment_id=comment.id,
                    github_client=github_client,
                    kind=lookup.kind,
                    pull_request_number=review_identity.pr_number,
                )
            except CliError as error:
                actions.append(
                    CloseAction(
                        kind=stack_comment_label(lookup.kind),
                        body=str(error),
                        status="blocked",
                    )
                )
                return tuple(actions), False
        action_body = (
            f"delete {stack_comment_label(lookup.kind)} #{comment.id} from "
            f"PR #{review_identity.pr_number}"
        )
        if not dry_run and not deleted:
            action_body = (
                f"{stack_comment_label(lookup.kind)} #{comment.id} already absent from "
                f"PR #{review_identity.pr_number}"
            )
        actions.append(
            CloseAction(
                kind=stack_comment_label(lookup.kind),
                body=action_body,
                status="planned" if dry_run else "applied",
            )
        )
    return tuple(actions), True


def _resolve_managed_comment_from_listed(
    *,
    comments: tuple[GithubIssueComment, ...],
    kind: StackCommentKind,
    pull_request_number: int,
) -> ManagedCommentLookup | None:
    matching_comments = [
        comment for comment in comments if comment_matches_kind(body=comment.body, kind=kind)
    ]
    if len(matching_comments) > 1:
        return ManagedCommentLookup(
            kind=kind,
            blocked_reason=(
                f"cannot delete {stack_comment_label(kind)}s because GitHub reports "
                f"multiple candidates on PR #{pull_request_number}"
            ),
        )
    if not matching_comments:
        return None
    return ManagedCommentLookup(kind=kind, comment=matching_comments[0])


def emit_close_actions(
    *,
    actions: tuple[CloseAction, ...],
    applied: bool,
    blocked: bool,
) -> None:
    header = (
        "Close blocked:"
        if blocked
        else ("Applied close actions:" if applied else "Planned close actions:")
    )
    console.output(header)
    for action in actions:
        emit_action_row(kind=action.kind, status=action.status, body=action.body)


def emit_action_row(
    *,
    kind: str,
    status: ActionPresentationStatus,
    body: Message,
) -> None:
    prefix, prefix_style, body_style = _action_presentation(status)
    message = body
    if kind != "tracking":
        message = (ui.semantic_text(kind, "prefix"), ": ", body)
    console.output(
        ui.prefixed_line(
            f"{prefix} ",
            message,
            prefix_labels=prefix_style,
            message_labels=body_style,
        )
    )


def _action_presentation(
    status: ActionPresentationStatus,
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


def plan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    review_identity: ReviewIdentity,
    change_id: str,
    local_commit_id: str | None,
    remote_commit_id: str | None,
    record_action: Callable[[CloseAction], None],
    remote_name: str | None,
) -> BookmarkCleanupPlan:
    """Validate managed branch identity and decide which cleanup mutations are safe."""

    if not bookmark_cleanup_allowed(
        bookmark=bookmark,
        change_id=change_id,
    ):
        record_action(
            CloseAction(
                kind="tracking",
                body=t"cannot clean up {ui.bookmark(bookmark)} because it does not match "
                t"change {ui.change_id(change_id)}",
                status="blocked",
            )
        )
        return BookmarkCleanupPlan(
            blocked=True,
            local_forget=False,
            remote_delete=False,
        )

    local_forget = False
    remote_delete = False
    local_conflict = False
    remote_conflict = False
    branch_label = f"{bookmark}@{remote_name}" if remote_name is not None else bookmark

    match classify_local_bookmark_forget(
        bookmark_state=bookmark_state,
        expected_commit_id=local_commit_id,
    ):
        case "conflicted" | "diverged" as local_safety:
            record_action(
                CloseAction(
                    kind="local bookmark",
                    body=local_bookmark_forget_blocked_body(bookmark, local_safety),
                    status="blocked",
                )
            )
            local_conflict = True
        case "safe":
            local_forget = True
        case _:
            pass

    remote_state = bookmark_state.remote_target(remote_name) if remote_name is not None else None
    if remote_commit_id is not None:
        review_status = classify_review_change(
            commit_id=remote_commit_id,
            local="orphaned",
            pull_request_lookup=None,
            remote_state=remote_state,
            review_identity=review_identity,
        )
        if review_status.remote_branch == "conflicted":
            record_action(
                CloseAction(
                    kind="remote branch",
                    body=t"cannot delete {ui.bookmark(branch_label)} because the remote "
                    t"bookmark is conflicted",
                    status="blocked",
                )
            )
            remote_conflict = True
        elif (
            review_status.remote_branch != "absent"
            and review_status.remote_branch_matches_commit is not True
        ):
            record_action(
                CloseAction(
                    kind="remote branch",
                    body=t"cannot delete {ui.bookmark(branch_label)} because it already "
                    t"points to a different revision",
                    status="blocked",
                )
            )
            remote_conflict = True
        elif review_status.remote_branch_matches_commit is True:
            remote_delete = True

    if local_conflict:
        remote_delete = False
    if remote_conflict:
        local_forget = False
    return BookmarkCleanupPlan(
        blocked=local_conflict or remote_conflict,
        local_forget=local_forget,
        remote_delete=remote_delete,
    )


async def native_stack_cleanup_blocker(
    *,
    delete_remote_branch: bool,
    github_client: GithubClient,
    persist: bool,
    pull_number: int,
    state_store: ReviewStateStore,
) -> CloseAction | None:
    """Fail closed when current native membership still needs a review branch."""

    if not delete_remote_branch:
        return None
    try:
        await GithubStackSelection(
            github_client, (pull_number,), state_store
        ).require_unstacked(persist=persist)
    except CliError as error:
        return CloseAction(kind="remote branch", body=str(error), status="blocked")
    return None


async def authorize_current_review_cleanup(
    *,
    change_id: str,
    delete_remote_branch: bool,
    github_client: GithubClient,
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    state_store: ReviewStateStore,
    submitted_baseline: SubmittedBaseline,
) -> CloseAction | None:
    """Authorize one review cleanup against every fresh remote authority."""

    _pull_request, blocker = await authorize_current_tracked_pull_request(
        allowed_states=frozenset({"closed", "merged"}),
        change_id=change_id,
        github_client=github_client,
        require_no_open_dependents=True,
        retry_command=retry_command,
        review_identity=review_identity,
        state_store=state_store,
        submitted_baseline=submitted_baseline,
    )
    if blocker is not None:
        return blocker
    return await native_stack_cleanup_blocker(
        delete_remote_branch=delete_remote_branch,
        github_client=github_client,
        persist=False,
        pull_number=review_identity.pr_number,
        state_store=state_store,
    )


def apply_bookmark_cleanup(
    *,
    bookmark: str,
    change_id: str,
    cleanup_plan: BookmarkCleanupPlan,
    local_commit_id: str | None,
    remote_commit_id: str | None,
    record_action: Callable[[CloseAction], None],
    remote_name: str | None,
    review_identity: ReviewIdentity,
    run: BookmarkCleanupRun,
) -> bool:
    """Re-read, then execute validated bookmark cleanup mutations."""

    dry_run = run.dry_run
    if not dry_run:
        latest_state = run.jj_client.get_bookmark_state(bookmark)
        if remote_name is not None:
            remote_branches = run.jj_client.list_remote_branches(
                remote=remote_name,
                patterns=(f"refs/heads/{bookmark}",),
            )
            remote_target = remote_branches.get(bookmark)
            other_remotes = tuple(
                state for state in latest_state.remote_targets if state.remote != remote_name
            )
            latest_state = latest_state.model_copy(
                update={
                    "remote_targets": (
                        *other_remotes,
                        RemoteBookmarkState(
                            remote=remote_name,
                            targets=(() if remote_target is None else (remote_target,)),
                        ),
                    )
                }
            )
        latest_plan = plan_bookmark_cleanup(
            bookmark=bookmark,
            bookmark_state=latest_state,
            change_id=change_id,
            local_commit_id=local_commit_id,
            record_action=record_action,
            remote_commit_id=remote_commit_id,
            remote_name=remote_name,
            review_identity=review_identity,
        )
        if latest_plan.blocked:
            return False
        if latest_plan != cleanup_plan:
            record_action(
                CloseAction(
                    kind="tracking",
                    body=t"bookmark state for {ui.bookmark(bookmark)} changed during cleanup; "
                    t"reload and retry",
                    status="blocked",
                )
            )
            return False
    if cleanup_plan.remote_delete:
        branch_label = f"{bookmark}@{remote_name}" if remote_name is not None else bookmark
        record_action(
            CloseAction(
                kind="remote branch",
                body=t"delete {ui.bookmark(branch_label)}",
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            if remote_name is None or remote_commit_id is None:
                raise AssertionError("Planned remote branch deletion requires a target.")
            run.jj_client.delete_remote_bookmarks(
                remote=remote_name,
                deletions=((bookmark, remote_commit_id),),
            )
    if cleanup_plan.local_forget:
        record_action(
            CloseAction(
                kind="local bookmark",
                body=t"forget {ui.bookmark(bookmark)}",
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            run.jj_client.forget_bookmarks((bookmark,))
    return True
