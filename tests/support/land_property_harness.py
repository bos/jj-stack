"""Runner-agnostic replay helpers for land property scenarios."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jj_stack.errors import EXIT_INCOMPLETE
from jj_stack.jj.client import JjClient
from jj_stack.models.review_state import ReviewState
from jj_stack.state.store import ReviewStateStore

from .fake_github import FakeGithubRepository
from .integration_helpers import commit_file, run_command, write_file
from .land_property_scenarios import (
    BYSTANDER_LABELS,
    INSERTED_LABEL,
    LandEditOperation,
    LandScenario,
    filename_for_land_label,
    subject_for_land_label,
)

CliRunner = Callable[[tuple[str, ...]], int]
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

    labels_to_change_ids = _capture_initial_labels(repo=repo, scenario=scenario)
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
            scenario=scenario,
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
    repo: Path,
    scenario: LandScenario,
) -> dict[str, str]:
    stack = JjClient(repo).discover_review_stack()
    if len(stack.revisions) != scenario.initial_size:
        raise AssertionError((scenario.trace, len(stack.revisions)))
    labels_to_change_ids: dict[str, str] = {}
    for label, revision in zip(scenario.initial_labels, stack.revisions, strict=True):
        assert revision.subject == subject_for_land_label(label), (label, scenario.trace)
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
    scenario: LandScenario,
) -> None:
    exit_code = run_cli(("list", "--json"))
    captured = read_output()
    assert exit_code in (0, EXIT_INCOMPLETE), (scenario.trace, captured.out, captured.err)

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
    assert set(landed_change_ids).isdisjoint(listed_change_ids), scenario.trace
    assert set(remaining_tracked_change_ids) <= listed_change_ids, scenario.trace


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
