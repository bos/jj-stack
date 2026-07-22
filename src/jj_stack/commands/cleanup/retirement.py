"""Merged-ancestor retirement planning and execution for cleanup rebase."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.jj.client import JjClient
from jj_stack.models.bookmarks import BookmarkState
from jj_stack.models.stack import LocalRevision
from jj_stack.review.bookmarks import bookmark_cleanup_allowed
from jj_stack.review.change_status import classify_review_status_revision
from jj_stack.review.status import PreparedStatus, ReviewStatusRevision

from .shared import CleanupAction, PreparedRebase, _revision_label_template


@dataclass(frozen=True, slots=True)
class MergedAncestorRetirement:
    """One merged revision proven safe to retire."""

    cleanup_review_bookmark: bool
    revision: ReviewStatusRevision


@dataclass(frozen=True, slots=True)
class MergedAncestorRetirementPlan:
    """Pure retirement decisions for the merged revisions on one selected path."""

    preservation_actions: tuple[CleanupAction, ...]
    retirements: tuple[MergedAncestorRetirement, ...]


def retire_merged_ancestors(
    *,
    blocked: bool,
    client: JjClient,
    merged_revisions: tuple[ReviewStatusRevision, ...],
    prepared_rebase: PreparedRebase,
    prepared_status: PreparedStatus,
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Plan and apply conservative retirement after survivor rebases finish."""

    if blocked or not merged_revisions:
        return

    prepared = prepared_status.prepared
    plan = plan_merged_ancestor_retirements(
        bookmark_states=client.list_bookmark_states(),
        cleanup_user_bookmarks=(prepared_rebase.context.config.cleanup_user_bookmarks),
        local_revisions_by_change_id={
            prepared_revision.revision.change_id: prepared_revision.revision
            for prepared_revision in prepared.status_revisions
        },
        merged_revisions=merged_revisions,
        prefix=prepared_rebase.context.config.bookmark_prefix,
    )
    for action in plan.preservation_actions:
        record_action(action)
    _apply_merged_ancestor_retirement_plan(
        client=client,
        plan=plan,
        prepared_rebase=prepared_rebase,
        prepared_status=prepared_status,
        record_action=record_action,
    )


def plan_merged_ancestor_retirements(
    *,
    bookmark_states: Mapping[str, BookmarkState],
    cleanup_user_bookmarks: bool,
    local_revisions_by_change_id: Mapping[str, LocalRevision],
    merged_revisions: tuple[ReviewStatusRevision, ...],
    prefix: str,
) -> MergedAncestorRetirementPlan:
    """Prove which merged local copies are inert without performing I/O.

    The proof requires the reviewed commit, one visible mutable revision, unambiguous
    bookmark identity, and cleanup permission for every local bookmark that abandonment
    would remove. Anything short of that proof remains in place with an explanation.
    """

    preservation_actions: list[CleanupAction] = []
    retirements: list[MergedAncestorRetirement] = []

    for revision in merged_revisions:
        review_identity = revision.review_identity
        submitted_baseline = revision.submitted_baseline
        local_revision = local_revisions_by_change_id.get(revision.change_id)
        if (
            review_identity is None
            or submitted_baseline is None
            or local_revision is None
            or submitted_baseline.commit_id != local_revision.commit_id
        ):
            # A rewritten merged change is already reported as a blocking rebase
            # condition before retirement planning. Missing proof stays untouched.
            continue
        remote_state = revision.remote_state
        if remote_state is not None and len(remote_state.targets) > 1:
            preservation_actions.append(
                CleanupAction(
                    kind="abandon",
                    status="skipped",
                    body=(
                        t"preserve merged {_revision_label_template(revision)}: remote "
                        t"bookmark {ui.bookmark(revision.bookmark)} is conflicted"
                    ),
                )
            )
            continue
        pointing_bookmarks = tuple(
            bookmark_state
            for bookmark_state in sorted(
                bookmark_states.values(),
                key=lambda candidate: candidate.name,
            )
            if local_revision.commit_id in bookmark_state.local_targets
        )
        conflicted_bookmark = next(
            (
                bookmark_state
                for bookmark_state in pointing_bookmarks
                if len(bookmark_state.local_targets) > 1
            ),
            None,
        )
        if conflicted_bookmark is not None:
            preservation_actions.append(
                CleanupAction(
                    kind="abandon",
                    status="skipped",
                    body=(
                        t"preserve merged {_revision_label_template(revision)}: local "
                        t"bookmark {ui.bookmark(conflicted_bookmark.name)} is conflicted"
                    ),
                )
            )
            continue
        if classify_review_status_revision(revision).local == "divergent":
            preservation_actions.append(
                CleanupAction(
                    kind="abandon",
                    status="skipped",
                    body=(
                        t"preserve merged {_revision_label_template(revision)}: multiple "
                        t"visible revisions still share that change ID"
                    ),
                )
            )
            continue
        if local_revision.immutable:
            retire_command = f"unstack --cleanup --pull-request {revision.pull_request_number()}"
            preservation_actions.append(
                CleanupAction(
                    kind="abandon",
                    status="skipped",
                    body=(
                        t"preserve merged {_revision_label_template(revision)}: the local "
                        t"commit is immutable; run {ui.cmd(retire_command)} to retire "
                        t"its review"
                    ),
                )
            )
            continue
        cleanup_review_bookmark = bookmark_cleanup_allowed(
            bookmark=revision.bookmark,
            bookmark_managed=review_identity.manages_bookmark,
            cleanup_user_bookmarks=cleanup_user_bookmarks,
            prefix=prefix,
        )
        guarded_bookmark = next(
            (
                bookmark_state
                for bookmark_state in pointing_bookmarks
                if not bookmark_cleanup_allowed(
                    bookmark=bookmark_state.name,
                    bookmark_managed=(
                        review_identity.manages_bookmark
                        if bookmark_state.name == revision.bookmark
                        else False
                    ),
                    cleanup_user_bookmarks=cleanup_user_bookmarks,
                    prefix=prefix,
                )
            ),
            None,
        )
        if guarded_bookmark is not None:
            preservation_actions.append(
                CleanupAction(
                    kind="abandon",
                    status="skipped",
                    body=(
                        t"preserve merged {_revision_label_template(revision)}: bookmark "
                        t"{ui.bookmark(guarded_bookmark.name)} is not managed by jj-stack "
                        t"(set {ui.cmd('jj-stack.cleanup_user_bookmarks=true')} to "
                        t"include it)"
                    ),
                )
            )
            continue
        retirements.append(
            MergedAncestorRetirement(
                cleanup_review_bookmark=cleanup_review_bookmark,
                revision=revision,
            )
        )

    return MergedAncestorRetirementPlan(
        preservation_actions=tuple(preservation_actions),
        retirements=tuple(retirements),
    )


def _apply_merged_ancestor_retirement_plan(
    *,
    client: JjClient,
    plan: MergedAncestorRetirementPlan,
    prepared_rebase: PreparedRebase,
    prepared_status: PreparedStatus,
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Apply remote deletion, local abandonment, then tracking removal."""

    if not plan.retirements:
        return

    dry_run = prepared_rebase.dry_run
    status = "planned" if dry_run else "applied"
    prepared = prepared_status.prepared
    remote = prepared.remote
    deletions = (
        ()
        if remote is None
        else tuple(
            (retirement.revision.bookmark, retirement.revision.remote_state.target)
            for retirement in plan.retirements
            if retirement.cleanup_review_bookmark
            and retirement.revision.remote_state is not None
            and retirement.revision.remote_state.target is not None
        )
    )
    if deletions:
        if remote is None:
            raise AssertionError("Remote branch deletions require a resolved remote.")
        if not dry_run:
            client.delete_remote_bookmarks(
                remote=remote.name,
                deletions=deletions,
                fetch=False,
            )
        for bookmark, _expected_remote_target in deletions:
            record_action(
                CleanupAction(
                    kind="remote branch",
                    status=status,
                    body=t"delete {ui.bookmark(bookmark)}@{remote.name}",
                )
            )

    if not dry_run:
        client.abandon_revisions(
            tuple(retirement.revision.change_id for retirement in plan.retirements)
        )
    for retirement in plan.retirements:
        revision = retirement.revision
        record_action(
            CleanupAction(
                kind="abandon",
                status=status,
                body=(
                    t"abandon merged {_revision_label_template(revision)}; its reviewed "
                    t"commit already landed through PR #{revision.pull_request_number()}"
                ),
            )
        )

    if not dry_run and deletions:
        if remote is None:
            raise AssertionError("Remote branch deletions require a resolved remote.")
        client.fetch_remote(remote=remote.name)

    if not dry_run:
        for retirement in plan.retirements:
            revision = retirement.revision
            if revision.review_identity is None or revision.submitted_baseline is None:
                raise AssertionError("Planned retirement requires complete tracking records.")
            prepared_rebase.context.state_store.retire_review(
                revision.change_id,
                expected_identity=revision.review_identity,
                expected_baseline=revision.submitted_baseline,
            )
    for retirement in plan.retirements:
        record_action(
            CleanupAction(
                kind="tracking",
                status=status,
                body=(
                    t"remove tracking for landed {_revision_label_template(retirement.revision)}"
                ),
            )
        )
