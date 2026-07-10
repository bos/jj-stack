"""Runner-agnostic replay helpers for land property scenarios."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jj_stack.errors import EXIT_FAILURE, EXIT_GITHUB, EXIT_INCOMPLETE
from jj_stack.jj.client import JjClient
from jj_stack.models.review_state import ReviewState
from jj_stack.state.journal import OPERATION_LOG_FILENAME, read_operation_log
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from .fake_github import FakeGithubRepository
from .integration_helpers import commit_file, run_command, write_file
from .land_property_scenarios import (
    BYSTANDER_LABELS,
    INSERTED_LABEL,
    LandDriftScenario,
    LandEditOperation,
    LandHandoffScenario,
    LandRetryScenario,
    LandScenario,
    filename_for_land_label,
    subject_for_land_label,
)
from .submit_property_harness import VIEW_REPORT_EXIT_CODES, advance_remote_trunk

CliRunner = Callable[[tuple[str, ...]], int]
CliErrorReader = Callable[[], BaseException | None]
OutputReader = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class _TrackedChange:
    bookmark: str
    change_id: str
    pull_number: int


@dataclass(frozen=True, slots=True)
class _BystanderSnapshot:
    """Everything about the second, unselected stack that land must not touch."""

    cached_records: dict[str, dict[str, Any]]
    pull_numbers: tuple[int, ...]
    pull_request_states: dict[int, tuple[str, str, str]]
    remote_refs: dict[str, str]


def replay_land_scenario(
    *,
    fake_repo: FakeGithubRepository,
    read_output: OutputReader,
    repo: Path,
    run_cli: CliRunner,
    scenario: LandScenario,
) -> None:
    """Replay one land scenario: edit, approve a prefix, land, assert the contract."""

    labels_to_change_ids = _capture_initial_labels(
        initial_labels=scenario.initial_labels,
        repo=repo,
        trace=scenario.trace,
    )
    bystander_snapshot: _BystanderSnapshot | None = None
    if scenario.with_second_stack:
        _create_bystander_stack(read_output=read_output, repo=repo, run_cli=run_cli)

    if scenario.edits:
        _apply_land_edits(
            labels_to_change_ids=labels_to_change_ids,
            repo=repo,
            scenario=scenario,
        )
        if scenario.resubmit_after_edit:
            head_change_id = labels_to_change_ids[scenario.final_live_labels[-1]]
            assert run_cli(("submit", head_change_id)) == 0, scenario.trace
            read_output()

    stack = _discover_final_stack(
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        scenario=scenario,
    )
    tracked = _capture_tracked_changes(
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        scenario=scenario,
    )
    _approve_ready_prefix(fake_repo=fake_repo, scenario=scenario, tracked=tracked)
    if scenario.unmergeable_pull_number is not None:
        fake_repo.unmergeable_pull_numbers.add(scenario.unmergeable_pull_number)
    if scenario.with_second_stack:
        bystander_snapshot = _capture_bystander_snapshot(fake_repo=fake_repo, repo=repo)

    current_commit_ids = {
        revision.change_id: revision.commit_id for revision in stack.revisions
    }
    original_main = _remote_ref(fake_repo.git_dir, "main")
    fake_repo.pull_request_events.clear()

    if scenario.land_target_position is not None:
        target_label = scenario.final_live_labels[scenario.land_target_position - 1]
        args = ["land", "--pull-request", str(tracked[target_label].pull_number)]
    else:
        args = ["land", stack.head.change_id]
    if scenario.via == "merge":
        args.extend(("--via", "merge"))
    if scenario.skip_cleanup:
        args.append("--skip-cleanup")
    exit_code = run_cli(tuple(args))
    captured = read_output()
    assert exit_code == scenario.expected_exit_code, (
        scenario.trace,
        captured.out,
        captured.err,
    )

    landed_labels = scenario.expected_landed_labels
    remaining_labels = scenario.final_live_labels[len(landed_labels) :]
    landed = tuple(tracked[label] for label in landed_labels)
    remaining_tracked = tuple(
        tracked[label] for label in remaining_labels if label in tracked
    )
    state = ReviewStateStore.for_repo(repo).load()

    untouched_pull_numbers = {
        change.pull_number for change in tracked.values()
    } - {change.pull_number for change in landed}
    if scenario.unmergeable_pull_number is not None:
        untouched_pull_numbers.discard(scenario.unmergeable_pull_number)
    if bystander_snapshot is not None:
        untouched_pull_numbers.update(bystander_snapshot.pull_numbers)

    if scenario.via == "push":
        assert_push_landing(
            current_commit_ids=current_commit_ids,
            fake_repo=fake_repo,
            landed=landed,
            original_main=original_main,
            remaining_tracked=remaining_tracked,
            repo=repo,
            skip_cleanup=scenario.skip_cleanup,
            state=state,
            trace=scenario.trace,
        )
        _assert_list_reflects_landed_prefix(
            landed_change_ids=tuple(change.change_id for change in landed),
            read_output=read_output,
            remaining_tracked_change_ids=tuple(
                change.change_id for change in remaining_tracked
            ),
            run_cli=run_cli,
            trace=scenario.trace,
        )
    else:
        _assert_merge_transport_result(
            current_commit_ids=current_commit_ids,
            fake_repo=fake_repo,
            landed=landed,
            original_main=original_main,
            remaining_tracked=remaining_tracked,
            repo=repo,
            state=state,
            trace=scenario.trace,
        )

    _assert_orphans_untouched(
        fake_repo=fake_repo,
        scenario=scenario,
        state=state,
        tracked=tracked,
    )
    assert_event_contract(
        fake_repo=fake_repo,
        landed_pull_numbers={change.pull_number for change in landed},
        trace=scenario.trace,
        untouched_pull_numbers=untouched_pull_numbers,
    )
    if bystander_snapshot is not None:
        _assert_bystander_untouched(
            fake_repo=fake_repo,
            repo=repo,
            snapshot=bystander_snapshot,
            trace=scenario.trace,
        )


def _capture_initial_labels(
    *,
    initial_labels: tuple[str, ...],
    repo: Path,
    trace: str,
) -> dict[str, str]:
    stack = JjClient(repo).discover_review_stack()
    if len(stack.revisions) != len(initial_labels):
        raise AssertionError((trace, len(stack.revisions)))
    labels_to_change_ids: dict[str, str] = {}
    for label, revision in zip(initial_labels, stack.revisions, strict=True):
        assert revision.subject == subject_for_land_label(label), (label, trace)
        labels_to_change_ids[label] = revision.change_id
    return labels_to_change_ids


def _create_bystander_stack(
    *,
    read_output: OutputReader,
    repo: Path,
    run_cli: CliRunner,
) -> dict[str, str]:
    """Create and submit a second independent stack from trunk."""

    run_command(["jj", "new", "main"], repo)
    for label in BYSTANDER_LABELS:
        commit_file(repo, subject_for_land_label(label), filename_for_land_label(label))
    stack = JjClient(repo).discover_review_stack()
    subjects = tuple(revision.subject for revision in stack.revisions)
    assert subjects == tuple(subject_for_land_label(label) for label in BYSTANDER_LABELS)
    assert run_cli(("submit", stack.head.change_id)) == 0
    read_output()
    return {
        label: revision.change_id
        for label, revision in zip(BYSTANDER_LABELS, stack.revisions, strict=True)
    }


def _capture_bystander_snapshot(
    *,
    fake_repo: FakeGithubRepository,
    repo: Path,
) -> _BystanderSnapshot:
    state = ReviewStateStore.for_repo(repo).load()
    cached_records: dict[str, dict[str, Any]] = {}
    pull_numbers: list[int] = []
    pull_request_states: dict[int, tuple[str, str, str]] = {}
    remote_refs: dict[str, str] = {}
    bystander_subjects = {
        subject_for_land_label(label) for label in BYSTANDER_LABELS
    }
    for change_id, cached in state.changes.items():
        pull_number = cached.pr_number
        if pull_number is None:
            continue
        pull_request = fake_repo.pull_requests[pull_number]
        if pull_request.title not in bystander_subjects:
            continue
        cached_records[change_id] = cached.model_dump()
        pull_numbers.append(pull_number)
        pull_request_states[pull_number] = (
            pull_request.state,
            pull_request.base_ref,
            pull_request.head_ref,
        )
        if cached.bookmark is not None:
            remote_refs[cached.bookmark] = _remote_ref(fake_repo.git_dir, cached.bookmark)
    assert len(pull_numbers) == len(BYSTANDER_LABELS)
    return _BystanderSnapshot(
        cached_records=cached_records,
        pull_numbers=tuple(sorted(pull_numbers)),
        pull_request_states=pull_request_states,
        remote_refs=remote_refs,
    )


def _assert_bystander_untouched(
    *,
    fake_repo: FakeGithubRepository,
    repo: Path,
    snapshot: _BystanderSnapshot,
    trace: str,
) -> None:
    state = ReviewStateStore.for_repo(repo).load()
    for change_id, expected_record in snapshot.cached_records.items():
        cached = state.changes.get(change_id)
        assert cached is not None, (change_id, trace)
        assert cached.model_dump() == expected_record, (change_id, trace)
    for pull_number, expected in snapshot.pull_request_states.items():
        pull_request = fake_repo.pull_requests[pull_number]
        actual = (pull_request.state, pull_request.base_ref, pull_request.head_ref)
        assert actual == expected, (pull_number, trace)
    for bookmark, expected_target in snapshot.remote_refs.items():
        assert _remote_ref(fake_repo.git_dir, bookmark) == expected_target, (
            bookmark,
            trace,
        )


def _apply_land_edits(
    *,
    labels_to_change_ids: dict[str, str],
    repo: Path,
    scenario: LandScenario,
) -> None:
    live = list(scenario.initial_labels)
    for operation in scenario.edits:
        live = _apply_one_land_edit(
            labels_to_change_ids=labels_to_change_ids,
            live_labels=live,
            operation=operation,
            repo=repo,
        )
    assert tuple(live) == scenario.final_live_labels, scenario.trace


def _apply_one_land_edit(
    *,
    labels_to_change_ids: dict[str, str],
    live_labels: list[str],
    operation: LandEditOperation,
    repo: Path,
) -> list[str]:
    change_id = labels_to_change_ids[operation.label]
    index = live_labels.index(operation.label)

    if operation.kind == "abandon":
        run_command(["jj", "abandon", change_id], repo)
        return [label for label in live_labels if label != operation.label]

    if operation.kind == "rewrite":
        run_command(["jj", "new", change_id], repo)
        write_file(
            repo / filename_for_land_label(operation.label),
            f"{subject_for_land_label(operation.label)} rewritten\n",
        )
        run_command(
            ["jj", "squash", "--into", change_id, "--use-destination-message"],
            repo,
        )
        return live_labels

    if operation.kind == "insert_after":
        next_label = live_labels[index + 1] if index + 1 < len(live_labels) else None
        run_command(["jj", "new", change_id], repo)
        commit_file(
            repo,
            subject_for_land_label(INSERTED_LABEL),
            filename_for_land_label(INSERTED_LABEL),
        )
        inserted_stack = JjClient(repo).discover_review_stack()
        labels_to_change_ids[INSERTED_LABEL] = inserted_stack.head.change_id
        if next_label is not None:
            run_command(
                [
                    "jj",
                    "rebase",
                    "-s",
                    labels_to_change_ids[next_label],
                    "-d",
                    labels_to_change_ids[INSERTED_LABEL],
                ],
                repo,
            )
        return [
            *live_labels[: index + 1],
            INSERTED_LABEL,
            *live_labels[index + 1 :],
        ]

    if operation.kind == "move_to_top":
        top_label = live_labels[-1]
        run_command(
            ["jj", "rebase", "-r", change_id, "-A", labels_to_change_ids[top_label]],
            repo,
        )
        return [
            *[label for label in live_labels if label != operation.label],
            operation.label,
        ]

    if operation.kind in {"move_after", "move_before"}:
        target_label = operation.target_label
        assert target_label is not None
        flag = "-A" if operation.kind == "move_after" else "-B"
        run_command(
            ["jj", "rebase", "-r", change_id, flag, labels_to_change_ids[target_label]],
            repo,
        )
        reordered = [label for label in live_labels if label != operation.label]
        target_index = reordered.index(target_label)
        insert_at = target_index + 1 if operation.kind == "move_after" else target_index
        reordered.insert(insert_at, operation.label)
        return reordered

    if operation.kind == "squash_into_previous":
        destination_label = live_labels[index - 1]
        run_command(
            [
                "jj",
                "squash",
                "--from",
                change_id,
                "--into",
                labels_to_change_ids[destination_label],
                "--use-destination-message",
            ],
            repo,
        )
        return [label for label in live_labels if label != operation.label]

    raise AssertionError(f"unsupported land edit: {operation.kind}")


def _discover_final_stack(
    *,
    labels_to_change_ids: dict[str, str],
    repo: Path,
    scenario: LandScenario,
):
    head_change_id = labels_to_change_ids[scenario.final_live_labels[-1]]
    stack = JjClient(repo).discover_review_stack(head_change_id)
    expected_change_ids = tuple(
        labels_to_change_ids[label] for label in scenario.final_live_labels
    )
    discovered_change_ids = tuple(revision.change_id for revision in stack.revisions)
    assert discovered_change_ids == expected_change_ids, scenario.trace
    return stack


def _capture_tracked_changes(
    *,
    labels_to_change_ids: dict[str, str],
    repo: Path,
    scenario: LandScenario,
) -> dict[str, _TrackedChange]:
    state = ReviewStateStore.for_repo(repo).load()
    tracked: dict[str, _TrackedChange] = {}
    for label in (*scenario.final_live_labels, *scenario.orphaned_labels):
        if not scenario.label_has_pull_request(label):
            continue
        change_id = labels_to_change_ids[label]
        cached = state.changes.get(change_id)
        if cached is None or cached.bookmark is None or cached.pr_number is None:
            raise AssertionError(("missing review identity", label, scenario.trace))
        tracked[label] = _TrackedChange(
            bookmark=cached.bookmark,
            change_id=change_id,
            pull_number=cached.pr_number,
        )
    return tracked


def _approve_ready_prefix(
    *,
    fake_repo: FakeGithubRepository,
    scenario: LandScenario,
    tracked: dict[str, _TrackedChange],
) -> None:
    for index, label in enumerate(scenario.final_live_labels):
        if index >= scenario.approved_prefix:
            break
        change = tracked.get(label)
        if change is None:
            # An inserted change without a resubmit has no PR to approve; the
            # modeled walk stops there anyway.
            continue
        fake_repo.create_pull_request_review(
            pull_number=change.pull_number,
            reviewer_login=f"land-reviewer-{label}",
            state="APPROVED",
        )


def assert_push_landing(
    *,
    current_commit_ids: dict[str, str],
    fake_repo: FakeGithubRepository,
    landed: tuple[_TrackedChange, ...],
    original_main: str,
    remaining_tracked: tuple[_TrackedChange, ...],
    repo: Path,
    skip_cleanup: bool,
    state: ReviewState,
    trace: str,
) -> None:
    """Assert the direct-push landing contract for an ordered landed prefix."""

    if landed:
        expected_main = current_commit_ids[landed[-1].change_id]
        assert _remote_ref(fake_repo.git_dir, "main") == expected_main, trace
    else:
        assert _remote_ref(fake_repo.git_dir, "main") == original_main, trace

    for change in landed:
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "closed", (change.pull_number, trace)
        assert pull_request.merged_at is not None, (change.pull_number, trace)
        assert change.change_id not in state.changes, (change.pull_number, trace)
        # Remote review branches for landed PRs stay intact at the landed commit.
        landed_commit = current_commit_ids[change.change_id]
        assert _remote_ref(fake_repo.git_dir, change.bookmark) == landed_commit, (
            change.pull_number,
            trace,
        )

    bookmark_states = JjClient(repo).list_bookmark_states(
        tuple(change.bookmark for change in landed)
    )
    for change in landed:
        local_target = bookmark_states[change.bookmark].local_target
        if skip_cleanup:
            assert local_target == current_commit_ids[change.change_id], (
                change.pull_number,
                trace,
            )
        else:
            assert local_target is None, (change.pull_number, trace)

    for change in remaining_tracked:
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "open", (change.pull_number, trace)
        assert pull_request.merged_at is None, (change.pull_number, trace)
        assert change.change_id in state.changes, (change.pull_number, trace)


def _assert_merge_transport_result(
    *,
    current_commit_ids: dict[str, str],
    fake_repo: FakeGithubRepository,
    landed: tuple[_TrackedChange, ...],
    original_main: str,
    remaining_tracked: tuple[_TrackedChange, ...],
    repo: Path,
    state: ReviewState,
    trace: str,
) -> None:
    if landed:
        assert _remote_ref(fake_repo.git_dir, "main") != original_main, trace
    else:
        assert _remote_ref(fake_repo.git_dir, "main") == original_main, trace

    # GitHub moved trunk by merging; the local commits stay untouched so a
    # follow-up sync or cleanup --rebase can remove the merged ancestors.
    client = JjClient(repo)
    for change_id, commit_id in current_commit_ids.items():
        assert client.resolve_revision(change_id).commit_id == commit_id, trace

    for change in landed:
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "closed", (change.pull_number, trace)
        assert pull_request.merged_at is not None, (change.pull_number, trace)
        cached = state.changes.get(change.change_id)
        assert cached is not None, (change.pull_number, trace)
        assert cached.pr_state == "merged", (change.pull_number, trace)

    for change in remaining_tracked:
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "open", (change.pull_number, trace)
        assert pull_request.merged_at is None, (change.pull_number, trace)
        cached = state.changes.get(change.change_id)
        assert cached is not None, (change.pull_number, trace)
        assert cached.pr_state == "open", (change.pull_number, trace)


def _assert_orphans_untouched(
    *,
    fake_repo: FakeGithubRepository,
    scenario: LandScenario,
    state: ReviewState,
    tracked: dict[str, _TrackedChange],
) -> None:
    for label in scenario.orphaned_labels:
        change = tracked.get(label)
        if change is None:
            # An inserted change abandoned mid-trace was never submitted, so
            # there is no orphaned review to protect.
            continue
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "open", (label, scenario.trace)
        assert pull_request.merged_at is None, (label, scenario.trace)
        cached = state.changes.get(change.change_id)
        assert cached is not None, (label, scenario.trace)
        assert cached.pr_number == change.pull_number, (label, scenario.trace)


def assert_event_contract(
    *,
    fake_repo: FakeGithubRepository,
    landed_pull_numbers: set[int],
    trace: str,
    untouched_pull_numbers: set[int],
) -> None:
    """Landed PRs transition to closed exactly once; untouched PRs see no event."""

    state_transitions: dict[int, list[Any]] = {}
    for event in fake_repo.pull_request_events:
        assert event.pull_request_number not in untouched_pull_numbers, (event, trace)
        if event.kind == "state":
            state_transitions.setdefault(event.pull_request_number, []).append(event)

    for pull_number in landed_pull_numbers:
        events = state_transitions.get(pull_number, ())
        assert len(events) == 1, (pull_number, events, trace)
        assert events[0].new_state == "closed", (pull_number, trace)
    for pull_number in state_transitions:
        assert pull_number in landed_pull_numbers, (pull_number, trace)


def _assert_list_reflects_landed_prefix(
    *,
    landed_change_ids: tuple[str, ...],
    read_output: OutputReader,
    remaining_tracked_change_ids: tuple[str, ...],
    run_cli: CliRunner,
    trace: str,
) -> None:
    exit_code = run_cli(("list", "--json"))
    captured = read_output()
    assert exit_code in (0, EXIT_INCOMPLETE), (trace, captured.out, captured.err)

    payload = json.loads(captured.out)
    rows = payload.get("rows", ())
    listed_change_ids = {
        change["change_id"]
        for row in rows
        for change in row.get("changes", ())
    }
    listed_change_ids.update(
        row["change_id"] for row in rows if row.get("type") == "orphan"
    )
    assert set(landed_change_ids).isdisjoint(listed_change_ids), trace
    assert set(remaining_tracked_change_ids) <= listed_change_ids, trace


@dataclass(frozen=True, slots=True)
class _BoundarySnapshot:
    """Every boundary a fail-closed land must leave untouched.

    Saved records are compared by review identity (bookmark, PR number, link
    state, last submitted commit) rather than full dumps: a blocked land may
    still refresh observed PR state into the cache, and that refresh is not a
    boundary mutation.
    """

    review_identities: dict[str, tuple[object, ...]]
    pull_request_states: dict[int, tuple[str, str, bool]]
    remote_refs: dict[str, str]


@dataclass(frozen=True, slots=True)
class _DriftStoppingChangeSnapshot:
    """Durable identity and GitHub state for the change that stops land."""

    change_id: str
    pull_number: int
    pull_request: tuple[object, ...]
    tracking_identity: tuple[object, ...]


def replay_land_drift_scenario(
    *,
    fake_repo: FakeGithubRepository,
    last_cli_error: CliErrorReader,
    read_output: OutputReader,
    repo: Path,
    run_cli: CliRunner,
    scenario: LandDriftScenario,
) -> None:
    """Replay one external transition against land and assert the modeled outcome."""

    labels_to_change_ids = _capture_initial_labels(
        initial_labels=scenario.initial_labels,
        repo=repo,
        trace=scenario.trace,
    )
    state = ReviewStateStore.for_repo(repo).load()
    tracked: dict[str, _TrackedChange] = {}
    for label in scenario.initial_labels:
        cached = state.changes[labels_to_change_ids[label]]
        assert cached.bookmark is not None and cached.pr_number is not None
        tracked[label] = _TrackedChange(
            bookmark=cached.bookmark,
            change_id=labels_to_change_ids[label],
            pull_number=cached.pr_number,
        )
        fake_repo.create_pull_request_review(
            pull_number=cached.pr_number,
            reviewer_login=f"land-reviewer-{label}",
            state="APPROVED",
        )

    _apply_land_drift(fake_repo=fake_repo, scenario=scenario, tracked=tracked)
    stopping_change_snapshot = (
        None
        if scenario.target_position is None
        else _capture_drift_stopping_change(
            fake_repo=fake_repo,
            repo=repo,
            tracked=tracked[scenario.initial_labels[scenario.target_position - 1]],
        )
    )
    boundary_snapshot = (
        _capture_boundary_snapshot(fake_repo=fake_repo, repo=repo, tracked=tracked)
        if scenario.outcome == "fail_closed"
        else None
    )
    original_main = _remote_ref(fake_repo.git_dir, "main")
    fake_repo.pull_request_events.clear()

    # Land runs on its default selection: the drifted states must survive the
    # in-command fetch, which can rewrite the local view before selection.
    exit_code = run_cli(("land",))
    captured = read_output()
    assert exit_code == scenario.expected_exit_code, (
        scenario.trace,
        captured.out,
        captured.err,
    )

    if scenario.outcome == "fail_closed":
        assert last_cli_error() is not None, scenario.trace
        assert fake_repo.pull_request_events == [], scenario.trace
        assert boundary_snapshot is not None
        _assert_boundaries_untouched(
            fake_repo=fake_repo,
            repo=repo,
            snapshot=boundary_snapshot,
            trace=scenario.trace,
        )
    else:
        assert scenario.target_position is not None
        landed = tuple(
            tracked[label] for label in scenario.expected_landed_labels
        )
        remaining_labels: tuple[str, ...] = ()
        if scenario.outcome == "prefix_stop":
            remaining_labels = scenario.initial_labels[scenario.target_position :]
        remaining_tracked = tuple(tracked[label] for label in remaining_labels)
        # The in-command fetch may rewrite surviving commits (for example after
        # jj abandons a change whose review branch was deleted), so resolve
        # the landed commits from the post-land view.
        client = JjClient(repo)
        current_commit_ids = {
            change.change_id: client.resolve_revision(change.change_id).commit_id
            for change in landed
        }
        assert_push_landing(
            current_commit_ids=current_commit_ids,
            fake_repo=fake_repo,
            landed=landed,
            original_main=original_main,
            remaining_tracked=remaining_tracked,
            repo=repo,
            skip_cleanup=False,
            state=ReviewStateStore.for_repo(repo).load(),
            trace=scenario.trace,
        )
        landed_pull_numbers = {change.pull_number for change in landed}
        assert_event_contract(
            fake_repo=fake_repo,
            landed_pull_numbers=landed_pull_numbers,
            trace=scenario.trace,
            untouched_pull_numbers={
                change.pull_number for change in tracked.values()
            }
            - landed_pull_numbers,
        )
        assert stopping_change_snapshot is not None
        _assert_drift_stopping_change_preserved(
            fake_repo=fake_repo,
            repo=repo,
            snapshot=stopping_change_snapshot,
            trace=scenario.trace,
        )

    view_exit_code = run_cli(("view",))
    read_output()
    assert view_exit_code in VIEW_REPORT_EXIT_CODES, (scenario.trace, view_exit_code)


def _apply_land_drift(
    *,
    fake_repo: FakeGithubRepository,
    scenario: LandDriftScenario,
    tracked: dict[str, _TrackedChange],
) -> None:
    if scenario.kind == "trunk_advanced":
        advance_remote_trunk(fake_repo)
        return

    assert scenario.target_position is not None
    label = scenario.initial_labels[scenario.target_position - 1]
    change = tracked[label]
    pull_request = fake_repo.pull_requests[change.pull_number]

    if scenario.kind == "pr_merged_externally":
        fake_repo.apply_squash_merge(pull_request)
        pull_request.merged_at = (
            datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        fake_repo.update_pull_request_state(
            pull_request, state="closed", reason="external_merge"
        )
        return
    if scenario.kind == "pr_closed":
        fake_repo.update_pull_request_state(
            pull_request, state="closed", reason="external_close"
        )
        return
    if scenario.kind == "review_branch_deleted":
        # GitHub closes a pull request when its head branch is deleted, so the
        # faithful transition is branch deletion plus PR closure.
        run_command(
            [
                "git",
                "--git-dir",
                str(fake_repo.git_dir),
                "update-ref",
                "-d",
                f"refs/heads/{change.bookmark}",
            ],
            fake_repo.git_dir.parent,
        )
        fake_repo.update_pull_request_state(
            pull_request, state="closed", reason="head_branch_deleted"
        )
        return
    if scenario.kind == "pr_draft_toggled":
        pull_request.is_draft = True
        return
    if scenario.kind == "changes_requested":
        # The same reviewer flips from approval to changes requested, so the
        # latest opinionated review controls the decision.
        fake_repo.create_pull_request_review(
            pull_number=change.pull_number,
            reviewer_login=f"land-reviewer-{label}",
            state="CHANGES_REQUESTED",
        )
        return
    raise AssertionError(f"unsupported land drift kind: {scenario.kind}")


def _capture_boundary_snapshot(
    *,
    fake_repo: FakeGithubRepository,
    repo: Path,
    tracked: dict[str, _TrackedChange],
) -> _BoundarySnapshot:
    state = ReviewStateStore.for_repo(repo).load()
    review_identities = {
        change.change_id: _review_identity(state, change.change_id)
        for change in tracked.values()
    }
    pull_request_states = {}
    remote_refs = {"main": _remote_ref(fake_repo.git_dir, "main")}
    for change in tracked.values():
        pull_request = fake_repo.pull_requests[change.pull_number]
        pull_request_states[change.pull_number] = (
            pull_request.state,
            pull_request.base_ref,
            bool(pull_request.is_draft),
        )
        remote_refs[change.bookmark] = _remote_ref(fake_repo.git_dir, change.bookmark)
    return _BoundarySnapshot(
        review_identities=review_identities,
        pull_request_states=pull_request_states,
        remote_refs=remote_refs,
    )


def _capture_drift_stopping_change(
    *,
    fake_repo: FakeGithubRepository,
    repo: Path,
    tracked: _TrackedChange,
) -> _DriftStoppingChangeSnapshot:
    state = ReviewStateStore.for_repo(repo).load()
    return _DriftStoppingChangeSnapshot(
        change_id=tracked.change_id,
        pull_number=tracked.pull_number,
        pull_request=_pull_request_identity(fake_repo, tracked.pull_number),
        tracking_identity=_durable_tracking_identity(state, tracked.change_id),
    )


def _assert_drift_stopping_change_preserved(
    *,
    fake_repo: FakeGithubRepository,
    repo: Path,
    snapshot: _DriftStoppingChangeSnapshot,
    trace: str,
) -> None:
    state = ReviewStateStore.for_repo(repo).load()
    assert snapshot.change_id in state.changes, (snapshot.change_id, trace)
    actual_tracking = _durable_tracking_identity(state, snapshot.change_id)
    assert actual_tracking == snapshot.tracking_identity, (
        snapshot.change_id,
        snapshot.tracking_identity,
        actual_tracking,
        trace,
    )
    assert _pull_request_identity(fake_repo, snapshot.pull_number) == snapshot.pull_request, (
        snapshot.pull_number,
        trace,
    )


def _durable_tracking_identity(state: ReviewState, change_id: str) -> tuple[object, ...]:
    cached = state.changes[change_id]
    return (
        cached.bookmark,
        cached.bookmark_ownership,
        cached.last_submitted_commit_id,
        cached.last_submitted_parent_change_id,
        cached.last_submitted_stack_head_change_id,
        cached.link_state,
        cached.pr_number,
        cached.pr_url,
    )


def _pull_request_identity(
    fake_repo: FakeGithubRepository,
    pull_number: int,
) -> tuple[object, ...]:
    pull_request = fake_repo.pull_requests[pull_number]
    reviews = fake_repo.list_pull_request_reviews(pull_number)
    return (
        pull_request.base_ref,
        pull_request.head_ref,
        pull_request.state,
        pull_request.merged_at,
        pull_request.is_draft,
        tuple((review.reviewer_login, review.state) for review in reviews),
    )


def _review_identity(state: ReviewState, change_id: str) -> tuple[object, ...]:
    cached = state.changes[change_id]
    return (
        cached.bookmark,
        cached.pr_number,
        cached.link_state,
        cached.last_submitted_commit_id,
    )


def _assert_boundaries_untouched(
    *,
    fake_repo: FakeGithubRepository,
    repo: Path,
    snapshot: _BoundarySnapshot,
    trace: str,
) -> None:
    state = ReviewStateStore.for_repo(repo).load()
    for change_id, expected_identity in snapshot.review_identities.items():
        assert change_id in state.changes, (change_id, trace)
        assert _review_identity(state, change_id) == expected_identity, (change_id, trace)
    for pull_number, expected in snapshot.pull_request_states.items():
        pull_request = fake_repo.pull_requests[pull_number]
        actual = (
            pull_request.state,
            pull_request.base_ref,
            bool(pull_request.is_draft),
        )
        assert actual == expected, (pull_number, trace)
    for bookmark, expected_target in snapshot.remote_refs.items():
        assert _remote_ref(fake_repo.git_dir, bookmark) == expected_target, (
            bookmark,
            trace,
        )


def replay_land_retry_scenario(
    *,
    fake_repo: FakeGithubRepository,
    install_fault: Callable[[], None],
    read_output: OutputReader,
    repo: Path,
    restore_github: Callable[[], None],
    run_cli: CliRunner,
    scenario: LandRetryScenario,
) -> None:
    """Interrupt one direct-push land at a checkpoint, rerun, and assert convergence."""

    labels_to_change_ids = _capture_initial_labels(
        initial_labels=scenario.initial_labels,
        repo=repo,
        trace=scenario.trace,
    )
    stack = JjClient(repo).discover_review_stack()
    current_commit_ids = {
        revision.change_id: revision.commit_id for revision in stack.revisions
    }
    state = ReviewStateStore.for_repo(repo).load()
    tracked: dict[str, _TrackedChange] = {}
    for label in scenario.initial_labels:
        cached = state.changes[labels_to_change_ids[label]]
        assert cached.bookmark is not None and cached.pr_number is not None
        tracked[label] = _TrackedChange(
            bookmark=cached.bookmark,
            change_id=labels_to_change_ids[label],
            pull_number=cached.pr_number,
        )
    for label in scenario.landed_labels:
        fake_repo.create_pull_request_review(
            pull_number=tracked[label].pull_number,
            reviewer_login=f"land-reviewer-{label}",
            state="APPROVED",
        )
    landed = tuple(tracked[label] for label in scenario.landed_labels)
    original_main = _remote_ref(fake_repo.git_dir, "main")
    fake_repo.pull_request_events.clear()

    if scenario.fault == "after_retire":
        exit_code = run_cli(("land",))
        captured = read_output()
        assert exit_code == 0, (scenario.trace, captured.out, captured.err)
        _drop_land_completed_marker(repo=repo, trace=scenario.trace)
    else:
        install_fault()
        exit_code = run_cli(("land",))
        captured = read_output()
        expected_exit_code = (
            EXIT_FAILURE if scenario.fault == "after_push_ack_lost" else EXIT_GITHUB
        )
        assert exit_code == expected_exit_code, (
            scenario.trace,
            captured.out,
            captured.err,
        )
        # The trunk push precedes finalization, so the interrupted run already
        # moved trunk to the last landed commit.
        expected_main = current_commit_ids[landed[-1].change_id]
        assert _remote_ref(fake_repo.git_dir, "main") == expected_main, scenario.trace
        restore_github()

    rerun_exit_code = run_cli(("land",))
    captured = read_output()
    assert rerun_exit_code == 0, (scenario.trace, captured.out, captured.err)

    remaining_tracked = tuple(
        tracked[label]
        for label in scenario.initial_labels[scenario.approved_prefix :]
    )
    assert_push_landing(
        current_commit_ids=current_commit_ids,
        fake_repo=fake_repo,
        landed=landed,
        original_main=original_main,
        remaining_tracked=remaining_tracked,
        repo=repo,
        skip_cleanup=False,
        state=ReviewStateStore.for_repo(repo).load(),
        trace=scenario.trace,
    )
    # The event window spans both runs, so exactly-once closure proves the
    # rerun finalized only what the interrupted run left unfinished.
    landed_pull_numbers = {change.pull_number for change in landed}
    assert_event_contract(
        fake_repo=fake_repo,
        landed_pull_numbers=landed_pull_numbers,
        trace=scenario.trace,
        untouched_pull_numbers={change.pull_number for change in tracked.values()}
        - landed_pull_numbers,
    )
    land_events = tuple(
        event
        for event in read_operation_log(resolve_state_path(repo).parent)
        if event.operation == "land"
    )
    assert land_events[-1].event == "completed", scenario.trace
    _assert_list_reflects_landed_prefix(
        landed_change_ids=tuple(change.change_id for change in landed),
        read_output=read_output,
        remaining_tracked_change_ids=tuple(
            change.change_id for change in remaining_tracked
        ),
        run_cli=run_cli,
        trace=scenario.trace,
    )


def replay_land_handoff_scenario(
    *,
    fake_repo: FakeGithubRepository,
    install_fault: Callable[[], None],
    read_output: OutputReader,
    repo: Path,
    restore_github: Callable[[], None],
    run_cli: CliRunner,
    scenario: LandHandoffScenario,
) -> None:
    """Replay one merged-prefix handoff chain and assert end-to-end convergence."""

    labels_to_change_ids = _capture_initial_labels(
        initial_labels=scenario.initial_labels,
        repo=repo,
        trace=scenario.trace,
    )
    state = ReviewStateStore.for_repo(repo).load()
    tracked: dict[str, _TrackedChange] = {}
    for label in scenario.initial_labels:
        cached = state.changes[labels_to_change_ids[label]]
        assert cached.bookmark is not None and cached.pr_number is not None
        tracked[label] = _TrackedChange(
            bookmark=cached.bookmark,
            change_id=labels_to_change_ids[label],
            pull_number=cached.pr_number,
        )
    for position, label in enumerate(scenario.initial_labels, start=1):
        if position == scenario.withheld_position:
            continue
        fake_repo.create_pull_request_review(
            pull_number=tracked[label].pull_number,
            reviewer_login=f"land-reviewer-{label}",
            state="APPROVED",
        )

    _apply_handoff_origin(
        fake_repo=fake_repo,
        install_fault=install_fault,
        read_output=read_output,
        restore_github=restore_github,
        run_cli=run_cli,
        scenario=scenario,
        tracked=tracked,
    )
    # One event window from here to the end of the chain: the merged prefix
    # must never see another event, and each suffix PR closes exactly once.
    fake_repo.pull_request_events.clear()

    _run_handoff_recovery(read_output=read_output, run_cli=run_cli, scenario=scenario)
    _assert_recovery_converged(
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        tracked=tracked,
    )

    if scenario.withheld_position is not None:
        withheld_label = scenario.initial_labels[scenario.withheld_position - 1]
        fake_repo.create_pull_request_review(
            pull_number=tracked[withheld_label].pull_number,
            reviewer_login=f"land-reviewer-{withheld_label}",
            state="APPROVED",
        )

    suffix = tuple(tracked[label] for label in scenario.suffix_labels)
    client = JjClient(repo)
    current_commit_ids = {
        change.change_id: client.resolve_revision(change.change_id).commit_id
        for change in suffix
    }
    original_main = _remote_ref(fake_repo.git_dir, "main")

    exit_code = run_cli(("land",))
    captured = read_output()
    assert exit_code == 0, (scenario.trace, captured.out, captured.err)

    assert_push_landing(
        current_commit_ids=current_commit_ids,
        fake_repo=fake_repo,
        landed=suffix,
        original_main=original_main,
        remaining_tracked=(),
        repo=repo,
        skip_cleanup=False,
        state=ReviewStateStore.for_repo(repo).load(),
        trace=scenario.trace,
    )
    assert_event_contract(
        fake_repo=fake_repo,
        landed_pull_numbers={change.pull_number for change in suffix},
        trace=scenario.trace,
        untouched_pull_numbers={
            tracked[label].pull_number for label in scenario.merged_labels
        },
    )

    # A merge-transport land leaves the pre-merge copies immutable (pinned by
    # their untracked remote review branches), so their tracking stays until a
    # broader cleanup retires it; only then has the lifecycle converged.
    exit_code = run_cli(("cleanup",))
    captured = read_output()
    assert exit_code == 0, (scenario.trace, captured.out, captured.err)
    final_state = ReviewStateStore.for_repo(repo).load()
    for change in tracked.values():
        assert change.change_id not in final_state.changes, (
            change.change_id,
            scenario.trace,
        )
    _assert_list_reflects_landed_prefix(
        landed_change_ids=tuple(change.change_id for change in tracked.values()),
        read_output=read_output,
        remaining_tracked_change_ids=(),
        run_cli=run_cli,
        trace=scenario.trace,
    )


def _apply_handoff_origin(
    *,
    fake_repo: FakeGithubRepository,
    install_fault: Callable[[], None],
    read_output: OutputReader,
    restore_github: Callable[[], None],
    run_cli: CliRunner,
    scenario: LandHandoffScenario,
    tracked: dict[str, _TrackedChange],
) -> None:
    """Move the merged prefix to trunk through GitHub, by land or externally."""

    if scenario.origin == "external_squash_merge":
        # An ordinary external merge of a stacked PR: retarget it to trunk,
        # squash-merge it, let GitHub close it as merged, and let GitHub's
        # auto-delete remove the head branch. The branch deletion is what
        # later lets the fetch drop the local review bookmark, mirroring how
        # a merge-transport land forgets it directly.
        for label in scenario.merged_labels:
            change = tracked[label]
            pull_request = fake_repo.pull_requests[change.pull_number]
            fake_repo.update_pull_request_base(
                pull_request, base_ref="main", reason="external_retarget"
            )
            fake_repo.apply_squash_merge(pull_request)
            pull_request.merged_at = (
                datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )
            fake_repo.update_pull_request_state(
                pull_request, state="closed", reason="external_merge"
            )
            run_command(
                [
                    "git",
                    "--git-dir",
                    str(fake_repo.git_dir),
                    "update-ref",
                    "-d",
                    f"refs/heads/{change.bookmark}",
                ],
                fake_repo.git_dir.parent,
            )
    elif scenario.merge_fault:
        install_fault()
        exit_code = run_cli(("land", "--via", "merge"))
        captured = read_output()
        assert exit_code == EXIT_GITHUB, (scenario.trace, captured.out, captured.err)
        restore_github()
    else:
        exit_code = run_cli(("land", "--via", "merge"))
        captured = read_output()
        assert exit_code == 0, (scenario.trace, captured.out, captured.err)

    for label in scenario.merged_labels:
        pull_request = fake_repo.pull_requests[tracked[label].pull_number]
        assert pull_request.state == "closed", (label, scenario.trace)
        assert pull_request.merged_at is not None, (label, scenario.trace)


def _run_handoff_recovery(
    *,
    read_output: OutputReader,
    run_cli: CliRunner,
    scenario: LandHandoffScenario,
) -> None:
    if scenario.recovery == "sync":
        exit_code = run_cli(("sync",))
        captured = read_output()
        assert exit_code == 0, (scenario.trace, captured.out, captured.err)
        return
    exit_code = run_cli(("cleanup", "--rebase"))
    captured = read_output()
    assert exit_code == 0, (scenario.trace, captured.out, captured.err)
    exit_code = run_cli(("submit",))
    captured = read_output()
    assert exit_code == 0, (scenario.trace, captured.out, captured.err)


def _assert_recovery_converged(
    *,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: LandHandoffScenario,
    tracked: dict[str, _TrackedChange],
) -> None:
    """The recovery rebased and resubmitted the suffix onto the merged trunk."""

    state = ReviewStateStore.for_repo(repo).load()
    client = JjClient(repo)
    previous_bookmark: str | None = None
    for position, label in enumerate(scenario.suffix_labels):
        change = tracked[label]
        cached = state.changes.get(change.change_id)
        assert cached is not None, (label, scenario.trace)
        assert cached.pr_number == change.pull_number, (label, scenario.trace)
        assert cached.bookmark == change.bookmark, (label, scenario.trace)
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "open", (label, scenario.trace)
        expected_base = "main" if position == 0 else previous_bookmark
        assert pull_request.base_ref == expected_base, (label, scenario.trace)
        commit_id = client.resolve_revision(change.change_id).commit_id
        assert _remote_ref(fake_repo.git_dir, change.bookmark) == commit_id, (
            label,
            scenario.trace,
        )
        previous_bookmark = change.bookmark

    for label in scenario.merged_labels:
        pull_request = fake_repo.pull_requests[tracked[label].pull_number]
        assert pull_request.state == "closed", (label, scenario.trace)
        assert pull_request.merged_at is not None, (label, scenario.trace)

    if scenario.origin == "external_squash_merge":
        # The recovery's rebase pass proves the pre-merge local copies inert
        # (reviewed commit unchanged since submit) and retires them directly.
        for label in scenario.merged_labels:
            assert tracked[label].change_id not in state.changes, (
                label,
                scenario.trace,
            )

    # Approvals granted before the handoff stay attached to the same PRs.
    pre_approved = (
        scenario.suffix_labels
        if scenario.withheld_position is None
        else scenario.suffix_labels[1:]
    )
    for label in pre_approved:
        reviews = fake_repo.list_pull_request_reviews(tracked[label].pull_number)
        assert any(
            review.state == "APPROVED"
            and review.reviewer_login == f"land-reviewer-{label}"
            for review in reviews
        ), (label, scenario.trace)


def _drop_land_completed_marker(*, repo: Path, trace: str) -> None:
    """Reproduce a crash between tracking retirement and the completed marker."""

    log_path = resolve_state_path(repo).parent / OPERATION_LOG_FILENAME
    lines = log_path.read_text(encoding="utf-8").splitlines()
    dropped_event = json.loads(lines[-1])
    assert (dropped_event["operation"], dropped_event["event"]) == ("land", "completed"), (
        trace,
        dropped_event,
    )
    log_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")


def _remote_ref(remote: Path, bookmark: str) -> str:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{bookmark}"],
        capture_output=True,
        check=False,
        cwd=remote.parent,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"could not read remote ref {bookmark!r}:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed.stdout.strip()
