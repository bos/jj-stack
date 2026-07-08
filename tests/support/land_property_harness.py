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
    INSERTED_LABEL,
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
    if scenario.edit is not None:
        _apply_land_edit(
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

    current_commit_ids = {
        revision.change_id: revision.commit_id for revision in stack.revisions
    }
    original_main = _remote_ref(fake_repo.git_dir, "main")
    fake_repo.pull_request_events.clear()

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

    if scenario.via == "push":
        _assert_direct_push_result(
            current_commit_ids=current_commit_ids,
            fake_repo=fake_repo,
            original_main=original_main,
            read_output=read_output,
            repo=repo,
            run_cli=run_cli,
            scenario=scenario,
            tracked=tracked,
        )
    else:
        _assert_merge_transport_result(
            current_commit_ids=current_commit_ids,
            fake_repo=fake_repo,
            original_main=original_main,
            repo=repo,
            scenario=scenario,
            tracked=tracked,
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


def _apply_land_edit(
    *,
    labels_to_change_ids: dict[str, str],
    repo: Path,
    scenario: LandScenario,
) -> None:
    edit = scenario.edit
    assert edit is not None
    change_id = labels_to_change_ids[edit.label]

    if edit.kind == "abandon":
        run_command(["jj", "abandon", change_id], repo)
        return

    if edit.kind == "rewrite":
        run_command(["jj", "new", change_id], repo)
        write_file(
            repo / filename_for_land_label(edit.label),
            f"{subject_for_land_label(edit.label)} rewritten\n",
        )
        run_command(
            ["jj", "squash", "--into", change_id, "--use-destination-message"],
            repo,
        )
        return

    if edit.kind == "insert_after":
        initial_labels = scenario.initial_labels
        index = initial_labels.index(edit.label)
        next_label = initial_labels[index + 1] if index + 1 < len(initial_labels) else None
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
        return

    raise AssertionError(f"unsupported land edit: {edit.kind}")


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


def _assert_direct_push_result(
    *,
    current_commit_ids: dict[str, str],
    fake_repo: FakeGithubRepository,
    original_main: str,
    read_output: OutputReader,
    repo: Path,
    run_cli: CliRunner,
    scenario: LandScenario,
    tracked: dict[str, _TrackedChange],
) -> None:
    landed_labels = scenario.expected_landed_labels
    remaining_labels = scenario.final_live_labels[len(landed_labels) :]
    state = ReviewStateStore.for_repo(repo).load()

    if landed_labels:
        last_landed = tracked[landed_labels[-1]]
        expected_main = current_commit_ids[last_landed.change_id]
        assert _remote_ref(fake_repo.git_dir, "main") == expected_main, scenario.trace
    else:
        assert _remote_ref(fake_repo.git_dir, "main") == original_main, scenario.trace

    landed_pull_numbers: set[int] = set()
    for label in landed_labels:
        change = tracked[label]
        landed_pull_numbers.add(change.pull_number)
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "closed", (label, scenario.trace)
        assert pull_request.merged_at is not None, (label, scenario.trace)
        assert change.change_id not in state.changes, (label, scenario.trace)
        # Remote review branches for landed PRs stay intact at the landed commit.
        landed_commit = current_commit_ids[change.change_id]
        assert _remote_ref(fake_repo.git_dir, change.bookmark) == landed_commit, (
            label,
            scenario.trace,
        )

    bookmark_states = JjClient(repo).list_bookmark_states(
        tuple(tracked[label].bookmark for label in landed_labels)
    )
    for label in landed_labels:
        change = tracked[label]
        local_target = bookmark_states[change.bookmark].local_target
        if scenario.skip_cleanup:
            assert local_target == current_commit_ids[change.change_id], (
                label,
                scenario.trace,
            )
        else:
            assert local_target is None, (label, scenario.trace)

    for label in remaining_labels:
        change = tracked.get(label)
        if change is None:
            continue
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "open", (label, scenario.trace)
        assert pull_request.merged_at is None, (label, scenario.trace)
        assert change.change_id in state.changes, (label, scenario.trace)

    _assert_orphans_untouched(
        fake_repo=fake_repo,
        scenario=scenario,
        state=state,
        tracked=tracked,
    )
    _assert_transient_event_contract(
        fake_repo=fake_repo,
        landed_pull_numbers=landed_pull_numbers,
        scenario=scenario,
        tracked=tracked,
    )
    _assert_list_reflects_landed_prefix(
        landed_change_ids=tuple(tracked[label].change_id for label in landed_labels),
        read_output=read_output,
        remaining_tracked_change_ids=tuple(
            tracked[label].change_id for label in remaining_labels if label in tracked
        ),
        run_cli=run_cli,
        scenario=scenario,
    )


def _assert_merge_transport_result(
    *,
    current_commit_ids: dict[str, str],
    fake_repo: FakeGithubRepository,
    original_main: str,
    repo: Path,
    scenario: LandScenario,
    tracked: dict[str, _TrackedChange],
) -> None:
    landed_labels = scenario.expected_landed_labels
    remaining_labels = scenario.final_live_labels[len(landed_labels) :]
    state = ReviewStateStore.for_repo(repo).load()

    if landed_labels:
        assert _remote_ref(fake_repo.git_dir, "main") != original_main, scenario.trace
    else:
        assert _remote_ref(fake_repo.git_dir, "main") == original_main, scenario.trace

    # GitHub moved trunk by merging; the local commits stay untouched so a
    # follow-up sync or cleanup --rebase can remove the merged ancestors.
    client = JjClient(repo)
    for change_id, commit_id in current_commit_ids.items():
        assert client.resolve_revision(change_id).commit_id == commit_id, scenario.trace

    landed_pull_numbers: set[int] = set()
    for label in landed_labels:
        change = tracked[label]
        landed_pull_numbers.add(change.pull_number)
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "closed", (label, scenario.trace)
        assert pull_request.merged_at is not None, (label, scenario.trace)
        cached = state.changes.get(change.change_id)
        assert cached is not None, (label, scenario.trace)
        assert cached.pr_state == "merged", (label, scenario.trace)

    for label in remaining_labels:
        change = tracked.get(label)
        if change is None:
            continue
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "open", (label, scenario.trace)
        assert pull_request.merged_at is None, (label, scenario.trace)
        cached = state.changes.get(change.change_id)
        assert cached is not None, (label, scenario.trace)
        assert cached.pr_state == "open", (label, scenario.trace)

    _assert_orphans_untouched(
        fake_repo=fake_repo,
        scenario=scenario,
        state=state,
        tracked=tracked,
    )
    _assert_transient_event_contract(
        fake_repo=fake_repo,
        landed_pull_numbers=landed_pull_numbers,
        scenario=scenario,
        tracked=tracked,
    )


def _assert_orphans_untouched(
    *,
    fake_repo: FakeGithubRepository,
    scenario: LandScenario,
    state: ReviewState,
    tracked: dict[str, _TrackedChange],
) -> None:
    for label in scenario.orphaned_labels:
        change = tracked[label]
        pull_request = fake_repo.pull_requests[change.pull_number]
        assert pull_request.state == "open", (label, scenario.trace)
        assert pull_request.merged_at is None, (label, scenario.trace)
        cached = state.changes.get(change.change_id)
        assert cached is not None, (label, scenario.trace)
        assert cached.pr_number == change.pull_number, (label, scenario.trace)


def _assert_transient_event_contract(
    *,
    fake_repo: FakeGithubRepository,
    landed_pull_numbers: set[int],
    scenario: LandScenario,
    tracked: dict[str, _TrackedChange],
) -> None:
    """Landed PRs transition to closed exactly once; no other original PR is touched.

    The merge transport legitimately retargets the first blocked PR to trunk
    before GitHub refuses the merge, so that PR is exempt from the untouched
    rule but must still never change state.
    """

    untouched_pull_numbers = {
        change.pull_number for change in tracked.values()
    } - landed_pull_numbers
    if scenario.unmergeable_pull_number is not None:
        untouched_pull_numbers.discard(scenario.unmergeable_pull_number)

    state_transitions: dict[int, list[Any]] = {}
    for event in fake_repo.pull_request_events:
        assert event.pull_request_number not in untouched_pull_numbers, (
            event,
            scenario.trace,
        )
        if event.kind == "state":
            state_transitions.setdefault(event.pull_request_number, []).append(event)

    for pull_number in landed_pull_numbers:
        events = state_transitions.get(pull_number, ())
        assert len(events) == 1, (pull_number, events, scenario.trace)
        assert events[0].new_state == "closed", (pull_number, scenario.trace)
    for pull_number in state_transitions:
        assert pull_number in landed_pull_numbers, (pull_number, scenario.trace)


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
