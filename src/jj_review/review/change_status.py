"""Derived per-change review lifecycle classification.

This module centralizes the observational state that commands derive from the
local `jj` stack, saved tracking data, bookmark observations, and GitHub PR
lookups. It deliberately does not mutate tracking state or decide command policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jj_review.models.bookmarks import RemoteBookmarkState
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalStack

LocalReviewState = Literal["present", "divergent", "orphaned", "missing"]
ReviewLinkState = Literal["untracked", "active", "unlinked"]
RemoteBranchReviewState = Literal[
    "absent",
    "current",
    "drifted",
    "conflicted",
    "untracked",
]
PullRequestLifecycle = Literal[
    "none",
    "open",
    "closed",
    "merged",
    "missing",
    "ambiguous",
]
PullRequestReviewDecision = Literal[
    "none",
    "approved",
    "changes_requested",
    "commented",
    "unknown",
]
BaselineFlag = Literal["commit_changed", "parent_changed", "stack_head_changed"]


@dataclass(frozen=True, slots=True)
class ReviewChangeStatus:
    """Orthogonal review state axes for one logical change."""

    local: LocalReviewState
    link: ReviewLinkState
    remote_branch: RemoteBranchReviewState
    remote_branch_matches_commit: bool | None
    pr_lifecycle: PullRequestLifecycle
    pr_draft: bool | None
    pr_review_decision: PullRequestReviewDecision
    baseline: frozenset[BaselineFlag] = frozenset()
    pr_lookup_error: bool = False
    pr_review_decision_error: str | None = None
    saved_review_identity: bool = False
    saved_pull_request_identity: bool = False

    @property
    def has_pull_request_lookup_failure(self) -> bool:
        """Whether GitHub PR inspection failed for this change."""

        return self.pr_lookup_error or self.pr_review_decision_error is not None

    @property
    def has_stale_pull_request_link(self) -> bool:
        """Whether saved PR identity exists but live branch lookup is missing."""

        return self.pr_lifecycle == "missing" and self.saved_pull_request_identity


@dataclass(frozen=True, slots=True)
class OrphanedRecord:
    """A saved tracking record whose change has left every live stack."""

    change_id: str
    cached_change: CachedChange


@dataclass(frozen=True, slots=True)
class SubmittedStateDisagreement:
    """One tracked change whose saved submit baseline no longer matches the DAG."""

    change_id: str
    commit_changed: bool = False
    parent_changed: bool = False
    stack_head_changed: bool = False


_OPEN_PR_STATES_FOR_ORPHANS = frozenset({"open", "draft"})


def classify_review_status_revision(
    revision: Any,
    *,
    baseline_disagreement: SubmittedStateDisagreement | None = None,
) -> ReviewChangeStatus:
    """Classify a rendered status revision without performing I/O."""

    local: LocalReviewState = (
        "divergent" if getattr(revision, "local_divergent", False) else "present"
    )
    return classify_review_change(
        baseline_disagreement=baseline_disagreement,
        cached_change=getattr(revision, "cached_change", None),
        commit_id=getattr(revision, "commit_id", None),
        link_state=getattr(revision, "link_state", None),
        local=local,
        pull_request_lookup=getattr(revision, "pull_request_lookup", None),
        remote_state=getattr(revision, "remote_state", None),
    )


def classify_review_change(
    *,
    cached_change: CachedChange | None,
    commit_id: str | None,
    local: LocalReviewState,
    pull_request_lookup: Any | None,
    remote_state: RemoteBookmarkState | None,
    link_state: str | None = None,
    baseline_disagreement: SubmittedStateDisagreement | None = None,
) -> ReviewChangeStatus:
    """Derive review status axes from already-loaded observations."""

    lifecycle, pr_lookup_error = _pull_request_lifecycle(pull_request_lookup)
    return ReviewChangeStatus(
        local=local,
        link=_link_state(cached_change, fallback=link_state),
        remote_branch=_remote_branch_state(
            commit_id=commit_id,
            remote_state=remote_state,
        ),
        remote_branch_matches_commit=_remote_branch_matches_commit(
            commit_id=commit_id,
            remote_state=remote_state,
        ),
        pr_lifecycle=lifecycle,
        pr_draft=_pull_request_draft(
            lifecycle=lifecycle,
            pull_request_lookup=pull_request_lookup,
        ),
        pr_review_decision=_pull_request_review_decision(
            lifecycle=lifecycle,
            pull_request_lookup=pull_request_lookup,
        ),
        baseline=_baseline_flags(baseline_disagreement),
        pr_lookup_error=pr_lookup_error,
        pr_review_decision_error=(
            None
            if pull_request_lookup is None
            else getattr(pull_request_lookup, "review_decision_error", None)
        ),
        saved_review_identity=_has_saved_review_identity(cached_change),
        saved_pull_request_identity=_has_saved_pull_request_identity(cached_change),
    )


def classify_saved_review_change(
    cached_change: CachedChange | None,
    *,
    local: LocalReviewState = "missing",
) -> ReviewChangeStatus:
    """Classify saved-only review state when live remote or PR data is not loaded."""

    return classify_review_change(
        cached_change=cached_change,
        commit_id=None,
        local=local,
        pull_request_lookup=None,
        remote_state=None,
    )


def is_open_pr_record(cached_change: CachedChange) -> bool:
    """Whether a saved record's PR is still open from tracking state alone.

    This is a tracking-state-only predicate: actively linked state, a saved PR
    number, and `pr_state` either open/draft or unknown. Callers that care
    whether the change has left live stacks must filter for that separately.
    """

    if _link_state(cached_change) != "active":
        return False
    if cached_change.pr_number is None:
        return False
    pr_state = cached_change.pr_state
    if pr_state is None:
        return True
    return pr_state in _OPEN_PR_STATES_FOR_ORPHANS


def enumerate_orphaned_records(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[OrphanedRecord, ...]:
    """Return saved open-PR records whose change is no longer in any live stack."""

    live_change_ids: set[str] = set()
    for stack in local_stacks:
        for revision in stack.revisions:
            live_change_ids.add(revision.change_id)

    orphans: list[OrphanedRecord] = []
    for change_id, cached_change in state.changes.items():
        if change_id in live_change_ids:
            continue
        if not is_open_pr_record(cached_change):
            continue
        orphans.append(OrphanedRecord(change_id=change_id, cached_change=cached_change))
    return tuple(orphans)


def submitted_state_disagreement(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[str, ...]:
    return tuple(
        disagreement.change_id
        for disagreement in submitted_state_disagreements(state, local_stacks)
    )


def submitted_state_disagreements(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[SubmittedStateDisagreement, ...]:
    """Return change_ids whose saved submitted state disagrees with the live DAG."""

    disagreements: list[SubmittedStateDisagreement] = []
    for stack in local_stacks:
        if not stack.revisions:
            continue
        live_head = stack.revisions[-1].change_id
        for index, revision in enumerate(stack.revisions):
            cached = state.changes.get(revision.change_id)
            if cached is None or cached.is_unlinked:
                continue
            commit_changed = _submitted_commit_disagrees(
                cached,
                revision_commit_id=revision.commit_id,
            )
            saved_parent = cached.last_submitted_parent_change_id
            saved_head = cached.last_submitted_stack_head_change_id
            parent_changed = False
            stack_head_changed = False
            if saved_parent is not None or saved_head is not None:
                live_parent = _live_parent_change_id(stack, index=index)
                parent_changed = saved_parent != live_parent
                stack_head_changed = saved_head != live_head
            if not commit_changed and not parent_changed and not stack_head_changed:
                continue
            disagreements.append(
                SubmittedStateDisagreement(
                    change_id=revision.change_id,
                    commit_changed=commit_changed,
                    parent_changed=parent_changed,
                    stack_head_changed=stack_head_changed,
                )
            )
    return tuple(disagreements)


def _link_state(
    cached_change: CachedChange | None,
    *,
    fallback: str | None = None,
) -> ReviewLinkState:
    if cached_change is None:
        if fallback == "unlinked":
            return "unlinked"
        return "untracked"
    if cached_change.is_unlinked:
        return "unlinked"
    return "active"


def _remote_branch_state(
    *,
    commit_id: str | None,
    remote_state: RemoteBookmarkState | None,
) -> RemoteBranchReviewState:
    if remote_state is None or not remote_state.targets:
        return "absent"
    if len(remote_state.targets) > 1:
        return "conflicted"
    if not remote_state.is_tracked:
        return "untracked"
    if commit_id is not None and remote_state.target == commit_id:
        return "current"
    return "drifted"


def _remote_branch_matches_commit(
    *,
    commit_id: str | None,
    remote_state: RemoteBookmarkState | None,
) -> bool | None:
    if commit_id is None or remote_state is None or len(remote_state.targets) != 1:
        return None
    return remote_state.target == commit_id


def _pull_request_lifecycle(
    pull_request_lookup: Any | None,
) -> tuple[PullRequestLifecycle, bool]:
    if pull_request_lookup is None:
        return "none", False
    lookup_state = getattr(pull_request_lookup, "state", "error")
    if lookup_state == "open":
        return "open", False
    if lookup_state == "closed":
        pull_request = getattr(pull_request_lookup, "pull_request", None)
        if pull_request is not None and getattr(pull_request, "state", None) == "merged":
            return "merged", False
        return "closed", False
    if lookup_state == "missing":
        return "missing", False
    if lookup_state == "ambiguous":
        return "ambiguous", False
    if lookup_state == "error":
        return "none", True
    return "none", True


def _pull_request_draft(
    *,
    lifecycle: PullRequestLifecycle,
    pull_request_lookup: Any | None,
) -> bool | None:
    if lifecycle != "open" or pull_request_lookup is None:
        return None
    pull_request = getattr(pull_request_lookup, "pull_request", None)
    if pull_request is None:
        return None
    return bool(getattr(pull_request, "is_draft", False))


def _pull_request_review_decision(
    *,
    lifecycle: PullRequestLifecycle,
    pull_request_lookup: Any | None,
) -> PullRequestReviewDecision:
    if lifecycle != "open" or pull_request_lookup is None:
        return "none"
    if getattr(pull_request_lookup, "review_decision_error", None) is not None:
        return "unknown"
    decision = getattr(pull_request_lookup, "review_decision", None)
    if decision is None:
        return "none"
    if decision in {"approved", "changes_requested", "commented"}:
        return decision
    return "unknown"


def _baseline_flags(
    disagreement: SubmittedStateDisagreement | None,
) -> frozenset[BaselineFlag]:
    if disagreement is None:
        return frozenset()
    flags: set[BaselineFlag] = set()
    if disagreement.commit_changed:
        flags.add("commit_changed")
    if disagreement.parent_changed:
        flags.add("parent_changed")
    if disagreement.stack_head_changed:
        flags.add("stack_head_changed")
    return frozenset(flags)


def _has_saved_pull_request_identity(cached_change: CachedChange | None) -> bool:
    return cached_change is not None and (
        cached_change.pr_number is not None or cached_change.pr_url is not None
    )


def _has_saved_review_identity(cached_change: CachedChange | None) -> bool:
    return cached_change is not None and cached_change.has_review_identity


def _submitted_commit_disagrees(
    cached_change: CachedChange,
    *,
    revision_commit_id: str,
) -> bool:
    saved_commit_id = cached_change.last_submitted_commit_id
    return saved_commit_id is not None and saved_commit_id != revision_commit_id


def _live_parent_change_id(stack: LocalStack, *, index: int) -> str | None:
    """Return the live review parent change_id for one stack revision."""

    if index > 0:
        return stack.revisions[index - 1].change_id

    base_parent = stack.base_parent
    if stack.base_parent_is_trunk_ancestor or base_parent.commit_id == stack.trunk.commit_id:
        return None
    if not base_parent.is_reviewable(allow_divergent=True):
        return None
    return base_parent.change_id
