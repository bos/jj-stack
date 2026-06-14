"""Cleanup rebase planning and execution."""

from __future__ import annotations

from collections.abc import Callable

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._action_recorder import ActionRecorder
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.review.bookmarks import bookmark_glob, is_review_bookmark
from jj_stack.review.change_status import (
    classify_review_status_revision,
    classify_saved_review_change,
)
from jj_stack.review.selection import resolve_selected_revset
from jj_stack.review.status import (
    PreparedStack,
    PreparedStatus,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    status_preparation_cli_error,
    stream_status,
)
from jj_stack.state.journal import OperationJournal

from .shared import (
    CleanupAction,
    PreparedRebase,
    RebaseResult,
    _build_action_streamer,
    _ClassifiedCleanupRebaseRevision,
    _emit_output_lines,
    _emit_severity_lines,
    _rebase_destination_template,
    _RebaseOperationPlan,
    _render_rebase_action_header,
    _render_rebase_postamble,
    _render_rebase_preamble,
    _revision_label_template,
)


def _run_cleanup_rebase_command(
    *,
    context: CommandContext,
    dry_run: bool,
    rebase_revset: str,
) -> int:
    """Render and run the `cleanup --rebase` command path."""

    try:
        with console.spinner(description="Inspecting jj stack"):
            prepared_rebase = _prepare_cleanup_rebase(
                context=context,
                dry_run=dry_run,
                rebase_revset=rebase_revset,
            )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    _emit_severity_lines(_render_rebase_preamble(prepared_rebase=prepared_rebase))

    try:
        result = _stream_rebase(
            on_action=_build_action_streamer(
                header=_render_rebase_action_header(dry_run=prepared_rebase.dry_run),
            ),
            prepared_rebase=prepared_rebase,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    _emit_output_lines(_render_rebase_postamble(result=result))
    return 1 if result.blocked else 0


def _prepare_cleanup_rebase(
    *,
    context: CommandContext,
    dry_run: bool,
    rebase_revset: str,
) -> PreparedRebase:
    selected_revset = resolve_selected_revset(
        command_label="cleanup --rebase --dry-run" if dry_run else "cleanup --rebase",
        default_revset="@-",
        require_explicit=False,
        revset=rebase_revset,
    )
    return PreparedRebase(
        context=context,
        dry_run=dry_run,
        prepared_status=prepare_status(
            context=context,
            fetch_remote_state=True,
            fetch_only_when_tracked=True,
            revset=selected_revset,
        ),
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
        if classify_saved_review_change(cached, local="present").saved_review_identity:
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
    prepared = prepared_status.prepared
    path_revisions = _resolve_rebase_path_revisions(
        prepared_status=prepared_status,
        status_result=status_result,
    )
    recorder = ActionRecorder[CleanupAction](on_action=on_action)

    if status_result.github_error is not None or status_result.github_repository is None:
        recorder.record(
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
            actions=recorder.as_tuple(),
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
        recorder.record(action)

    rebase_journal = _start_rebase_operation_log(
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
            rebase_plans=operation_plan.rebase_plans,
            record_action=recorder.record,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )

        _record_rebase_policy_actions(
            prefix=prepared_rebase.context.config.bookmark_prefix,
            merged_revisions=merged_revisions,
            record_action=recorder.record,
        )

        if not recorder.actions and merged_revisions:
            recorder.record(
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
            actions=recorder.as_tuple(),
            blocked=blocked,
        )
    finally:
        if _rebase_succeeded:
            ordered_change_ids = tuple(
                prepared_revision.revision.change_id
                for prepared_revision in prepared.status_revisions
            )
            rebase_journal.append(
                "completed",
                {"ordered_change_ids": ordered_change_ids},
            )


def _start_rebase_operation_log(
    *,
    blocked: bool,
    prepared: PreparedStack,
    prepared_rebase: PreparedRebase,
    selected_revset: str,
) -> OperationJournal:
    """Write a rebase operation log entry before live rebases begin."""

    if blocked or prepared_rebase.dry_run:
        return OperationJournal.disabled()

    ordered_change_ids = tuple(
        str(prepared_revision.revision.change_id)
        for prepared_revision in prepared.status_revisions
    )
    ordered_commit_ids = tuple(
        str(prepared_revision.revision.commit_id)
        for prepared_revision in prepared.status_revisions
    )
    state_dir = prepared.state_store.require_writable()
    journal = OperationJournal.begin(
        state_dir,
        operation="cleanup-rebase",
        options={},
        resolved_scope={
            "ordered_change_ids": ordered_change_ids,
            "ordered_commit_ids": ordered_commit_ids,
            "selected_revset": selected_revset,
        },
    )
    return journal


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
    status_result: StatusResult,
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
    classified_path_revisions = tuple(
        _ClassifiedCleanupRebaseRevision(
            revision=revision,
            status=classify_review_status_revision(revision),
        )
        for revision in path_revisions
    )
    merged_revisions = tuple(
        classified
        for classified in classified_path_revisions
        if classified.status.pr_lifecycle == "merged"
    )
    closed_unmerged_revisions = tuple(
        classified
        for classified in classified_path_revisions
        if classified.status.pr_lifecycle == "closed"
    )
    classified_revisions_by_change_id = {
        classified.revision.change_id: classified
        for classified in classified_path_revisions
    }
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
        revisions_by_change_id=classified_revisions_by_change_id,
    )

    return _RebaseOperationPlan(
        blocked=blocked,
        closed_unmerged_revisions=tuple(
            classified.revision for classified in closed_unmerged_revisions
        ),
        merged_revisions=tuple(classified.revision for classified in merged_revisions),
        pre_actions=tuple(actions),
        rebase_plans=tuple(rebase_plans),
    )


def _collect_rebase_pre_actions(
    *,
    closed_unmerged_revisions: tuple[_ClassifiedCleanupRebaseRevision, ...],
    current_commit_id_by_change_id: dict[str, str],
    merged_revisions: tuple[_ClassifiedCleanupRebaseRevision, ...],
) -> tuple[bool, list[CleanupAction]]:
    """Record blocking rebase conditions before survivor planning begins."""

    blocked = False
    actions: list[CleanupAction] = []
    for classified in closed_unmerged_revisions:
        revision = classified.revision
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

    for classified in merged_revisions:
        revision = classified.revision
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
    revisions_by_change_id: dict[str, _ClassifiedCleanupRebaseRevision],
) -> tuple[bool, list[tuple[str, str | None]]]:
    """Plan survivor rebases after merged ancestors are removed from the path."""

    survivor_change_ids: list[str] = []
    rebase_plans: list[tuple[str, str | None]] = []
    for prepared_revision in prepared_status.prepared.status_revisions:
        classified = revisions_by_change_id.get(prepared_revision.revision.change_id)
        if classified is None:
            continue
        revision = classified.revision
        change_status = classified.status
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
            if desired_parent_change_id is not None:
                blocked = True
                actions.append(
                    CleanupAction(
                        kind="rebase",
                        status="blocked",
                        body=(
                            t"cannot automatically rebase {_revision_label_template(revision)} "
                            t"onto surviving change "
                            t"{ui.change_id(desired_parent_change_id)}; rebase manually "
                            t"with {ui.cmd('jj rebase')}"
                        ),
                    )
                )
                survivor_change_ids.append(revision.change_id)
                continue
            rebase_plans.append((revision.change_id, desired_parent_change_id))
        survivor_change_ids.append(revision.change_id)
    return blocked, rebase_plans


def _rebase_parent_is_merged(
    *,
    parent_commit_id: str | None,
    prepared_status: PreparedStatus,
    revisions_by_change_id: dict[str, _ClassifiedCleanupRebaseRevision],
) -> bool:
    for candidate in prepared_status.prepared.status_revisions:
        if candidate.revision.commit_id != parent_commit_id:
            continue
        classified = revisions_by_change_id.get(candidate.revision.change_id)
        return classified is not None and classified.status.pr_lifecycle == "merged"
    return False
