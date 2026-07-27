"""Shared types and rendering helpers for close command action rows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._native_stack_safety import GithubStackSelection
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.stack_comments import (
    StackCommentKind,
    comment_matches_kind,
    delete_stack_comment,
    stack_comment_label,
)
from jj_stack.jj.client import JjClient, ReviewRefUpdate
from jj_stack.models.github import GithubIssueComment, GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline
from jj_stack.review.branches import review_branch_matches_change
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
        elif pull_request.head.sha != submitted_baseline.commit_id:
            reason = (
                t"cannot mutate saved PR #{pull_request_number} because its head no longer "
                t"matches the saved submitted commit"
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


def plan_review_cleanup(
    *,
    allowed_states: frozenset[str],
    change_id: str,
    observation: RepositoryObservation,
    preview_closed_dependents: frozenset[int] = frozenset(),
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    submitted_baseline: SubmittedBaseline,
) -> tuple[GithubPullRequest | None, ReviewRefUpdate | None, CloseAction | None]:
    """Authorize exact cleanup facts and derive at most one leased ref deletion."""

    pull_request, blocker = authorize_tracked_review(
        allowed_states=allowed_states,
        change_id=change_id,
        observation=observation,
        preview_closed_dependents=preview_closed_dependents,
        require_no_open_dependents=True,
        retry_command=retry_command,
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )
    if blocker is not None or pull_request is None:
        return pull_request, None, blocker
    configured_repository = observation.configured_repository
    if (
        observation.remote is None
        or configured_repository is None
        or configured_repository.repository_key != review_identity.repository_key
    ):
        return (
            pull_request,
            None,
            CloseAction(
                kind="remote branch",
                body=t"cannot resolve the configured remote for saved PR "
                t"#{review_identity.pr_number}",
                status="blocked",
            ),
        )
    branch = review_identity.head_ref
    if not review_branch_matches_change(branch, change_id):
        return (
            pull_request,
            None,
            CloseAction(
                kind="tracking",
                body=t"cannot clean up {ui.bookmark(branch)} because it does not match "
                t"change {ui.change_id(change_id)}",
                status="blocked",
            ),
        )
    remote_target = observation.reviews[change_id].remote_review_target
    if remote_target is not None and remote_target != submitted_baseline.commit_id:
        return (
            pull_request,
            None,
            CloseAction(
                kind="remote branch",
                body=t"cannot delete {ui.bookmark(branch)} because it "
                t"already points to a different revision",
                status="blocked",
            ),
        )
    update = (
        None
        if remote_target is None
        else ReviewRefUpdate(
            branch=branch,
            expected_target=submitted_baseline.commit_id,
            desired_target=None,
        )
    )
    return pull_request, update, None


async def native_stack_cleanup_blocker(
    *,
    github_client: GithubClient,
    persist: bool,
    pull_number: int,
    state_store: ReviewStateStore,
) -> CloseAction | None:
    """Fail closed when current native membership still needs a review branch."""

    try:
        await GithubStackSelection(github_client, (pull_number,), state_store).require_unstacked(
            persist=persist
        )
    except CliError as error:
        return CloseAction(kind="remote branch", body=str(error), status="blocked")
    return None


async def authorize_current_review_cleanup(
    *,
    change_id: str,
    context: CommandContext,
    expected_update: ReviewRefUpdate | None,
    github_client: GithubClient,
    remote_name: str,
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    submitted_baseline: SubmittedBaseline,
) -> CloseAction | None:
    """Authorize one review cleanup against every fresh remote authority."""

    current_update, blocker = await prepare_current_review_cleanup(
        allowed_states=frozenset({"closed", "merged"}),
        change_id=change_id,
        context=context,
        github_client=github_client,
        remote_name=remote_name,
        retry_command=retry_command,
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )
    if blocker is not None:
        return blocker
    if current_update != expected_update:
        return CloseAction(
            kind="remote branch",
            body=t"remote branch {ui.bookmark(review_identity.head_ref)} changed during "
            t"cleanup; reload and retry",
            status="blocked",
        )
    return None


async def prepare_current_review_cleanup(
    *,
    allowed_states: frozenset[str],
    change_id: str,
    context: CommandContext,
    github_client: GithubClient,
    remote_name: str,
    retry_command: str = "cleanup",
    review_identity: ReviewIdentity,
    submitted_baseline: SubmittedBaseline,
) -> tuple[ReviewRefUpdate | None, CloseAction | None]:
    """Freshly authorize one cleanup and derive its exact leased ref deletion."""

    try:
        observation = await observe_reviews(
            change_ids=(change_id,),
            context=context,
            github_client=github_client,
            include_open_dependents=True,
            remote_name=remote_name,
        )
    except GithubClientError:
        return None, github_observation_blocker()
    _pull_request, update, blocker = plan_review_cleanup(
        allowed_states=allowed_states,
        change_id=change_id,
        observation=observation,
        retry_command=retry_command,
        review_identity=review_identity,
        submitted_baseline=submitted_baseline,
    )
    if blocker is not None:
        return None, blocker
    blocker = await native_stack_cleanup_blocker(
        github_client=github_client,
        persist=False,
        pull_number=review_identity.pr_number,
        state_store=context.state_store,
    )
    return update, blocker


def apply_remote_branch_cleanup(
    *,
    dry_run: bool,
    jj_client: JjClient,
    record_action: Callable[[CloseAction], None],
    remote_name: str,
    update: ReviewRefUpdate | None,
) -> None:
    """Execute one pre-authorized remote branch deletion with an exact lease.

    A rejected lease raises, so there is no failure for callers to branch on.
    """

    if update is not None:
        if not dry_run:
            jj_client.mutate_remote_review_refs(
                remote=remote_name,
                updates=(update,),
            )
        record_action(
            CloseAction(
                kind="remote branch",
                body=t"delete {ui.bookmark(f'{update.branch}@{remote_name}')}",
                status="planned" if dry_run else "applied",
            )
        )
