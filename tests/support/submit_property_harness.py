"""Runner-agnostic replay helpers for submit property scenarios."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jj_stack.errors import ConflictedStackError, DriftError
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.review_state import CachedChange
from jj_stack.state.store import ReviewStateStore

from .fake_github import FakeGithubRepository
from .integration_helpers import commit_file, run_command, write_file
from .submit_property_scenarios import (
    CrossStackSplitScenario,
    DriftOperation,
    ExternalDriftScenario,
    StackEditOperation,
    StackEditScenario,
    StackMergeScenario,
    StackMoveScenario,
    SubmitInvariants,
    SubmitRetryScenario,
    filename_for_label,
    initial_label,
    subject_for_label,
)

SubmitRunner = Callable[[str | None], int]
CliRunner = Callable[[tuple[str, ...]], int]
CliErrorReader = Callable[[], BaseException | None]
OutputDiscarder = Callable[[], Any]

# `view` must always produce a report or a targeted diagnostic for a drifted
# state: a healthy report (0), an unsupported-stack diagnostic (2), or an
# incomplete/needs-attention report (10). Anything else is a crash or an
# unclassified error leaking through the report path.
VIEW_REPORT_EXIT_CODES = frozenset({0, 2, 10})


@dataclass(frozen=True, slots=True)
class SubmittedBaseline:
    bookmark: str
    cached_change: CachedChange
    change_id: str
    pr_base_ref: str
    pr_number: int
    remote_target: str


def replay_successful_stack_edit_scenario(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: StackEditScenario,
    submit: SubmitRunner,
) -> None:
    """Replay one generated stack-edit scenario and assert successful invariants."""

    labels_to_change_ids = _create_initial_stack(repo, scenario.initial_size)

    assert submit(None) == 0
    discard_output()
    baseline = _capture_submitted_baseline(repo, fake_repo, labels_to_change_ids)
    _approve_initial_pull_requests(fake_repo, baseline)
    fake_repo.pull_request_events.clear()

    live_labels = list(initial_label(index) for index in range(1, scenario.initial_size + 1))
    for operation in scenario.operations:
        live_labels = _apply_stack_edit_operation(
            repo=repo,
            labels_to_change_ids=labels_to_change_ids,
            live_labels=live_labels,
            operation=operation,
        )

    assert tuple(live_labels) == scenario.final_live_labels
    stack = _discover_stack_for_labels(
        repo=repo,
        labels=scenario.final_live_labels,
        labels_to_change_ids=labels_to_change_ids,
    )
    assert submit(stack.head.change_id) == 0
    discard_output()

    _assert_successful_submit_invariants(
        baseline=baseline,
        fake_repo=fake_repo,
        invariants=scenario.invariants,
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        stack=stack,
        strict_base_events=False,
    )


def replay_external_drift_scenario(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    last_cli_error: CliErrorReader,
    repo: Path,
    run_cli: CliRunner,
    scenario: ExternalDriftScenario,
) -> None:
    """Replay one external-drift scenario and assert the model-predicted outcome.

    Fail-closed scenarios must stop with one of the drift kind's expected
    diagnoses — not merely the right exit code — and leave every boundary
    untouched: remote refs, the PR database, and the saved review identity of
    every submitted change. `last_cli_error` reads the error the CLI reported
    for the most recent `run_cli` call. Success scenarios must converge on the
    normal post-submit contract. Both end with a `view` report on the drifted
    selection, which must never crash.
    """

    labels_to_change_ids = _create_initial_stack(repo, scenario.initial_size)

    assert run_cli(("submit",)) == 0
    discard_output()
    baseline = _capture_submitted_baseline(repo, fake_repo, labels_to_change_ids)
    _approve_initial_pull_requests(fake_repo, baseline)

    live_labels = list(initial_label(index) for index in range(1, scenario.initial_size + 1))
    for operation in scenario.edit_operations:
        live_labels = _apply_stack_edit_operation(
            repo=repo,
            labels_to_change_ids=labels_to_change_ids,
            live_labels=live_labels,
            operation=operation,
        )
    assert tuple(live_labels) == scenario.final_live_labels

    submit_revset = labels_to_change_ids[scenario.final_live_labels[-1]]
    for drift in scenario.drifts:
        revset_override = _apply_drift_operation(
            baseline=baseline,
            drift=drift,
            fake_repo=fake_repo,
            labels_to_change_ids=labels_to_change_ids,
            repo=repo,
            run_cli=run_cli,
        )
        discard_output()
        if revset_override is not None:
            submit_revset = revset_override

    if scenario.expected_outcome == "fail_closed":
        before_refs = _remote_refs(fake_repo.git_dir)
        before_github = _github_snapshot(fake_repo)
        before_bookmarks = _bookmark_snapshot(repo)
        before_state = ReviewStateStore.for_repo(repo).load()
        fake_repo.pull_request_events.clear()

        exit_code = run_cli(("submit", submit_revset))
        diagnosis = _fail_closed_diagnosis(last_cli_error())
        discard_output()

        assert exit_code in scenario.expected_exit_codes, (exit_code, scenario.trace)
        assert diagnosis in scenario.expected_diagnoses, (diagnosis, scenario.trace)
        assert _remote_refs(fake_repo.git_dir) == before_refs, scenario.trace
        assert _github_snapshot(fake_repo) == before_github, scenario.trace
        assert fake_repo.pull_request_events == [], scenario.trace
        assert _bookmark_snapshot(repo) == before_bookmarks, scenario.trace
        assert ReviewStateStore.for_repo(repo).load() == before_state, scenario.trace
    else:
        stack = _discover_stack_for_labels(
            repo=repo,
            labels=scenario.final_live_labels,
            labels_to_change_ids=labels_to_change_ids,
        )
        fake_repo.pull_request_events.clear()

        assert run_cli(("submit", stack.head.change_id)) == 0, scenario.trace
        discard_output()

        _assert_successful_submit_invariants(
            baseline=baseline,
            fake_repo=fake_repo,
            invariants=scenario.invariants,
            labels_to_change_ids=labels_to_change_ids,
            repo=repo,
            stack=stack,
            strict_base_events=False,
        )

    view_exit_code = run_cli(("view", submit_revset))
    discard_output()
    assert view_exit_code in VIEW_REPORT_EXIT_CODES, (view_exit_code, scenario.trace)


def _fail_closed_diagnosis(error: BaseException | None) -> str | None:
    """Collapse the CLI's reported error to the scenario model's diagnosis vocabulary."""

    if isinstance(error, DriftError):
        return error.condition
    if isinstance(error, ConflictedStackError):
        return "conflicted_stack"
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, UnsupportedStackError):
            return f"unsupported_stack:{cause.reason}"
        cause = cause.__cause__
    return None


def replay_failed_submit_retry_scenario(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: SubmitRetryScenario,
    submit: SubmitRunner,
) -> None:
    """Replay one failed submit and assert rerunning converges the selected stack."""

    if scenario.needs_initial_submit:
        _replay_failed_resubmit(
            discard_output=discard_output,
            fake_repo=fake_repo,
            repo=repo,
            scenario=scenario,
            submit=submit,
        )
    else:
        _replay_failed_first_submit(
            discard_output=discard_output,
            fake_repo=fake_repo,
            repo=repo,
            scenario=scenario,
            submit=submit,
        )


def _replay_failed_first_submit(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: SubmitRetryScenario,
    submit: SubmitRunner,
) -> None:
    """The first submit failed mid-mutation; the rerun must build state from scratch."""

    labels_to_change_ids = _create_initial_stack(repo, scenario.initial_size)

    assert submit(None) != 0
    discard_output()

    assert submit(None) == 0
    discard_output()

    stack = _discover_stack_for_labels(
        repo=repo,
        labels=scenario.final_live_labels,
        labels_to_change_ids=labels_to_change_ids,
    )
    _assert_new_submit_invariants(
        fake_repo=fake_repo,
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        scenario=scenario,
        stack=stack,
    )
    _assert_retry_metadata(fake_repo)


def _replay_failed_resubmit(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: SubmitRetryScenario,
    submit: SubmitRunner,
) -> None:
    """A previously-submitted stack rebuilds review identity on a faulted resubmit."""

    labels_to_change_ids = _create_initial_stack(repo, scenario.initial_size)

    assert submit(None) == 0
    discard_output()
    baseline = _capture_submitted_baseline(repo, fake_repo, labels_to_change_ids)
    _approve_initial_pull_requests(fake_repo, baseline)
    _rewrite_pull_request_body(
        repo=repo,
        label=scenario.failure_label,
        labels_to_change_ids=labels_to_change_ids,
    )
    submit_revset = labels_to_change_ids[initial_label(scenario.initial_size)]
    fake_repo.pull_request_events.clear()

    assert submit(submit_revset) != 0
    discard_output()

    assert submit(submit_revset) == 0
    discard_output()

    stack = _discover_stack_for_labels(
        repo=repo,
        labels=scenario.final_live_labels,
        labels_to_change_ids=labels_to_change_ids,
    )
    _assert_successful_submit_invariants(
        baseline=baseline,
        fake_repo=fake_repo,
        invariants=scenario.invariants,
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        stack=stack,
        strict_base_events=False,
    )
    _assert_retry_metadata(fake_repo)


def replay_cross_stack_split_scenario(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: CrossStackSplitScenario,
    submit: SubmitRunner,
) -> None:
    """Replay a split-stack rewrite and assert only the selected stack is updated."""

    labels_to_change_ids = _create_initial_stack(repo, scenario.initial_size)

    assert submit(None) == 0
    discard_output()
    baseline = _capture_submitted_baseline(repo, fake_repo, labels_to_change_ids)
    _approve_initial_pull_requests(fake_repo, baseline)
    fake_repo.pull_request_events.clear()

    run_command(
        [
            "jj",
            "rebase",
            "-s",
            labels_to_change_ids[scenario.source_label],
            "-d",
            labels_to_change_ids[scenario.target_label],
        ],
        repo,
    )
    selected_stack = _discover_stack_for_labels(
        repo=repo,
        labels=scenario.selected_labels,
        labels_to_change_ids=labels_to_change_ids,
    )
    _discover_stack_for_labels(
        repo=repo,
        labels=scenario.deferred_stack_labels,
        labels_to_change_ids=labels_to_change_ids,
    )

    assert submit(selected_stack.head.change_id) == 0
    discard_output()

    _assert_successful_submit_invariants(
        baseline=baseline,
        fake_repo=fake_repo,
        invariants=scenario.invariants,
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        stack=selected_stack,
        strict_base_events=False,
    )
    _assert_deferred_stack_untouched(
        baseline=baseline,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
    )


def replay_stack_merge_scenario(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: StackMergeScenario,
    submit: SubmitRunner,
) -> None:
    """Replay two independently submitted stacks merged into one selected stack."""

    labels_to_change_ids = _create_labeled_stack(repo, scenario.first_stack_labels)
    assert submit(labels_to_change_ids[scenario.first_stack_labels[-1]]) == 0
    discard_output()

    labels_to_change_ids.update(
        _create_labeled_stack(repo, scenario.second_stack_labels)
    )
    assert submit(labels_to_change_ids[scenario.second_stack_labels[-1]]) == 0
    discard_output()

    baseline = _capture_submitted_baseline(repo, fake_repo, labels_to_change_ids)
    _approve_initial_pull_requests(fake_repo, baseline)
    fake_repo.pull_request_events.clear()

    run_command(
        [
            "jj",
            "rebase",
            "-s",
            labels_to_change_ids[scenario.source_label],
            "-d",
            labels_to_change_ids[scenario.target_label],
        ],
        repo,
    )
    merged_stack = _discover_stack_for_labels(
        repo=repo,
        labels=scenario.selected_labels,
        labels_to_change_ids=labels_to_change_ids,
    )

    assert submit(merged_stack.head.change_id) == 0
    discard_output()

    _assert_successful_submit_invariants(
        baseline=baseline,
        fake_repo=fake_repo,
        invariants=scenario.invariants,
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        stack=merged_stack,
        strict_base_events=True,
    )


def replay_stack_move_scenario(
    *,
    discard_output: OutputDiscarder,
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: StackMoveScenario,
    submit: SubmitRunner,
) -> None:
    """Replay moving one submitted change into another submitted stack."""

    labels_to_change_ids = _create_labeled_stack(repo, scenario.first_stack_labels)
    assert submit(labels_to_change_ids[scenario.first_stack_labels[-1]]) == 0
    discard_output()

    labels_to_change_ids.update(
        _create_labeled_stack(repo, scenario.second_stack_labels)
    )
    assert submit(labels_to_change_ids[scenario.second_stack_labels[-1]]) == 0
    discard_output()

    baseline = _capture_submitted_baseline(repo, fake_repo, labels_to_change_ids)
    _approve_initial_pull_requests(fake_repo, baseline)
    fake_repo.pull_request_events.clear()

    run_command(
        [
            "jj",
            "rebase",
            "-r",
            labels_to_change_ids[scenario.source_label],
            "-A" if scenario.placement == "after" else "-B",
            labels_to_change_ids[scenario.target_label],
        ],
        repo,
    )
    selected_stack = _discover_stack_for_labels(
        repo=repo,
        labels=scenario.selected_labels,
        labels_to_change_ids=labels_to_change_ids,
    )
    if scenario.deferred_stack_labels:
        _discover_stack_for_labels(
            repo=repo,
            labels=scenario.deferred_stack_labels,
            labels_to_change_ids=labels_to_change_ids,
        )

    assert submit(selected_stack.head.change_id) == 0
    discard_output()

    _assert_successful_submit_invariants(
        baseline=baseline,
        fake_repo=fake_repo,
        invariants=scenario.invariants,
        labels_to_change_ids=labels_to_change_ids,
        repo=repo,
        stack=selected_stack,
        strict_base_events=True,
    )
    _assert_deferred_labels_untouched(
        baseline=baseline,
        deferred_labels=scenario.deferred_labels,
        fake_repo=fake_repo,
        repo=repo,
    )


def _create_initial_stack(repo: Path, initial_size: int) -> dict[str, str]:
    labels = tuple(initial_label(index) for index in range(1, initial_size + 1))
    return _create_labeled_stack(repo, labels)


def _create_labeled_stack(repo: Path, labels: tuple[str, ...]) -> dict[str, str]:
    run_command(["jj", "new", "main"], repo)
    for label in labels:
        commit_file(repo, subject_for_label(label), filename_for_label(label))

    stack = JjClient(repo).discover_review_stack()
    assert tuple(revision.subject for revision in stack.revisions) == tuple(
        subject_for_label(label) for label in labels
    )
    return {
        label: revision.change_id for label, revision in zip(labels, stack.revisions, strict=True)
    }


def _capture_submitted_baseline(
    repo: Path,
    fake_repo: FakeGithubRepository,
    labels_to_change_ids: dict[str, str],
) -> dict[str, SubmittedBaseline]:
    state = ReviewStateStore.for_repo(repo).load()
    remote_heads = _remote_refs(fake_repo.git_dir)
    baseline: dict[str, SubmittedBaseline] = {}
    for label, change_id in labels_to_change_ids.items():
        cached_change = state.changes[change_id]
        bookmark = cached_change.bookmark
        pr_number = cached_change.pr_number
        assert bookmark is not None
        assert pr_number is not None
        pull_request = fake_repo.pull_requests[pr_number]
        baseline[label] = SubmittedBaseline(
            bookmark=bookmark,
            cached_change=cached_change,
            change_id=change_id,
            pr_base_ref=pull_request.base_ref,
            pr_number=pr_number,
            remote_target=_remote_head(remote_heads, bookmark),
        )
    return baseline


def _approve_initial_pull_requests(
    fake_repo: FakeGithubRepository,
    baseline: dict[str, SubmittedBaseline],
) -> None:
    for label, submitted in baseline.items():
        fake_repo.create_pull_request_review(
            pull_number=submitted.pr_number,
            reviewer_login=f"reviewer-{label}",
            state="APPROVED",
        )


def _apply_stack_edit_operation(
    *,
    repo: Path,
    labels_to_change_ids: dict[str, str],
    live_labels: list[str],
    operation: StackEditOperation,
) -> list[str]:
    if operation.kind == "move_to_top":
        index = live_labels.index(operation.label)
        top_label = live_labels[-1]
        run_command(
            [
                "jj",
                "rebase",
                "-r",
                labels_to_change_ids[operation.label],
                "-A",
                labels_to_change_ids[top_label],
            ],
            repo,
        )
        return [*live_labels[:index], *live_labels[index + 1 :], operation.label]

    if operation.kind == "move_after":
        if operation.target_label is None:
            raise AssertionError("move_after operation requires a target label.")
        run_command(
            [
                "jj",
                "rebase",
                "-r",
                labels_to_change_ids[operation.label],
                "-A",
                labels_to_change_ids[operation.target_label],
            ],
            repo,
        )
        live_labels = [label for label in live_labels if label != operation.label]
        target_index = live_labels.index(operation.target_label)
        return [
            *live_labels[: target_index + 1],
            operation.label,
            *live_labels[target_index + 1 :],
        ]

    if operation.kind == "move_before":
        if operation.target_label is None:
            raise AssertionError("move_before operation requires a target label.")
        run_command(
            [
                "jj",
                "rebase",
                "-r",
                labels_to_change_ids[operation.label],
                "-B",
                labels_to_change_ids[operation.target_label],
            ],
            repo,
        )
        live_labels = [label for label in live_labels if label != operation.label]
        target_index = live_labels.index(operation.target_label)
        return [
            *live_labels[:target_index],
            operation.label,
            *live_labels[target_index:],
        ]

    if operation.kind == "insert_after":
        if operation.new_label is None:
            raise AssertionError("insert_after operation requires a new label.")
        index = live_labels.index(operation.label)
        next_label = live_labels[index + 1] if index + 1 < len(live_labels) else None
        run_command(["jj", "new", labels_to_change_ids[operation.label]], repo)
        commit_file(
            repo,
            subject_for_label(operation.new_label),
            filename_for_label(operation.new_label),
        )
        inserted_stack = JjClient(repo).discover_review_stack()
        labels_to_change_ids[operation.new_label] = inserted_stack.head.change_id
        if next_label is not None:
            run_command(
                [
                    "jj",
                    "rebase",
                    "-s",
                    labels_to_change_ids[next_label],
                    "-d",
                    labels_to_change_ids[operation.new_label],
                ],
                repo,
            )
        return [
            *live_labels[: index + 1],
            operation.new_label,
            *live_labels[index + 1 :],
        ]

    if operation.kind == "insert_before":
        if operation.new_label is None:
            raise AssertionError("insert_before operation requires a new label.")
        index = live_labels.index(operation.label)
        run_command(["jj", "new", "-B", labels_to_change_ids[operation.label]], repo)
        commit_file(
            repo,
            subject_for_label(operation.new_label),
            filename_for_label(operation.new_label),
        )
        inserted_stack = JjClient(repo).discover_review_stack()
        labels_to_change_ids[operation.new_label] = inserted_stack.head.change_id
        return [
            *live_labels[:index],
            operation.new_label,
            *live_labels[index:],
        ]

    if operation.kind == "abandon":
        run_command(["jj", "abandon", labels_to_change_ids[operation.label]], repo)
        return [label for label in live_labels if label != operation.label]

    if operation.kind == "rewrite":
        run_command(["jj", "new", labels_to_change_ids[operation.label]], repo)
        write_file(
            repo / filename_for_label(operation.label),
            f"{subject_for_label(operation.label)} rewritten\n",
        )
        run_command(
            [
                "jj",
                "squash",
                "--into",
                labels_to_change_ids[operation.label],
                "--use-destination-message",
            ],
            repo,
        )
        return live_labels

    if operation.kind == "squash_into_previous":
        index = live_labels.index(operation.label)
        if index == 0:
            raise AssertionError("squash_into_previous requires a non-bottom label.")
        target_label = live_labels[index - 1]
        run_command(
            [
                "jj",
                "squash",
                "--from",
                labels_to_change_ids[operation.label],
                "--into",
                labels_to_change_ids[target_label],
                "--use-destination-message",
            ],
            repo,
        )
        return [label for label in live_labels if label != operation.label]

    raise AssertionError(f"unsupported stack edit operation: {operation.kind}")


def _rewrite_pull_request_body(
    *,
    repo: Path,
    label: str,
    labels_to_change_ids: dict[str, str],
) -> None:
    run_command(
        [
            "jj",
            "describe",
            "-r",
            labels_to_change_ids[label],
            "-m",
            f"{subject_for_label(label)}\n\nupdated body",
        ],
        repo,
    )


def _discover_stack_for_labels(
    *,
    repo: Path,
    labels: tuple[str, ...],
    labels_to_change_ids: dict[str, str],
):
    head_change_id = labels_to_change_ids[labels[-1]]
    stack = JjClient(repo).discover_review_stack(head_change_id)
    expected_change_ids = tuple(labels_to_change_ids[label] for label in labels)
    assert tuple(revision.change_id for revision in stack.revisions) == expected_change_ids
    return stack


def _assert_new_submit_invariants(
    *,
    fake_repo: FakeGithubRepository,
    labels_to_change_ids: dict[str, str],
    repo: Path,
    scenario: SubmitRetryScenario,
    stack,
) -> None:
    state = ReviewStateStore.for_repo(repo).load()
    remote_heads = _remote_refs(fake_repo.git_dir)
    bookmarks_by_label: dict[str, str] = {}
    stack_head_change_id = labels_to_change_ids[scenario.final_live_labels[-1]]

    for index, label in enumerate(scenario.final_live_labels):
        revision = stack.revisions[index]
        cached_change = state.changes[revision.change_id]
        bookmark = cached_change.bookmark
        pr_number = cached_change.pr_number
        assert bookmark is not None, scenario.trace
        assert pr_number is not None, scenario.trace
        bookmarks_by_label[label] = bookmark

        pull_request = fake_repo.pull_requests[pr_number]
        expected_parent_change_id = (
            labels_to_change_ids[scenario.final_live_labels[index - 1]]
            if index > 0
            else None
        )
        expected_base_ref = (
            bookmarks_by_label[scenario.final_live_labels[index - 1]]
            if index > 0
            else "main"
        )
        assert _remote_head(remote_heads, bookmark) == revision.commit_id
        assert pull_request.base_ref == expected_base_ref
        assert pull_request.head_ref == bookmark
        assert pull_request.merged_at is None
        assert pull_request.state == "open"
        assert pull_request.title == subject_for_label(label)
        assert cached_change.last_submitted_commit_id == revision.commit_id
        assert cached_change.last_submitted_parent_change_id == expected_parent_change_id
        assert cached_change.last_submitted_stack_head_change_id == stack_head_change_id

    assert len(fake_repo.pull_requests) == scenario.initial_size


def _assert_successful_submit_invariants(
    *,
    baseline: dict[str, SubmittedBaseline],
    fake_repo: FakeGithubRepository,
    invariants: SubmitInvariants,
    labels_to_change_ids: dict[str, str],
    repo: Path,
    stack,
    strict_base_events: bool,
) -> None:
    state = ReviewStateStore.for_repo(repo).load()
    remote_heads = _remote_refs(fake_repo.git_dir)
    revisions_by_label = dict(zip(invariants.final_live_labels, stack.revisions, strict=True))
    expected_base_by_pr_number: dict[int, str] = {}
    live_pr_numbers: set[int] = set()
    bookmarks_by_label: dict[str, str] = {}
    stack_head_change_id = labels_to_change_ids[invariants.final_live_labels[-1]]

    for index, label in enumerate(invariants.final_live_labels):
        revision = revisions_by_label[label]
        cached_change = state.changes[revision.change_id]
        bookmark = cached_change.bookmark
        pr_number = cached_change.pr_number
        assert bookmark is not None, invariants.trace
        assert pr_number is not None, invariants.trace
        bookmarks_by_label[label] = bookmark
        live_pr_numbers.add(pr_number)
        if label in baseline:
            assert bookmark == baseline[label].bookmark, invariants.trace
            assert pr_number == baseline[label].pr_number, invariants.trace
            _assert_approval_review_preserved(fake_repo, pr_number, label)
        else:
            assert pr_number not in {submitted.pr_number for submitted in baseline.values()}

        pull_request = fake_repo.pull_requests[pr_number]
        expected_parent_change_id = (
            labels_to_change_ids[invariants.final_live_labels[index - 1]] if index > 0 else None
        )
        expected_base_ref = (
            bookmarks_by_label[invariants.final_live_labels[index - 1]] if index > 0 else "main"
        )
        expected_base_by_pr_number[pr_number] = expected_base_ref
        assert _remote_head(remote_heads, bookmark) == revision.commit_id
        assert pull_request.base_ref == expected_base_ref
        assert pull_request.head_ref == bookmark
        assert pull_request.merged_at is None
        assert pull_request.state == "open"
        assert pull_request.title == subject_for_label(label)
        assert cached_change.last_submitted_commit_id == revision.commit_id
        assert cached_change.last_submitted_parent_change_id == expected_parent_change_id
        assert cached_change.last_submitted_stack_head_change_id == stack_head_change_id

    for label in invariants.orphaned_labels:
        submitted = baseline[label]
        cached_change = state.changes[submitted.change_id]
        pull_request = fake_repo.pull_requests[submitted.pr_number]
        assert submitted.pr_number not in live_pr_numbers
        assert cached_change.bookmark == submitted.bookmark
        assert cached_change.pr_number == submitted.pr_number
        assert _remote_head(remote_heads, submitted.bookmark) == submitted.remote_target
        assert pull_request.base_ref == submitted.pr_base_ref
        assert pull_request.head_ref == submitted.bookmark
        assert pull_request.merged_at is None
        assert pull_request.state == "open"
        _assert_approval_review_preserved(fake_repo, submitted.pr_number, label)

    expected_pr_count = invariants.initial_size + sum(
        1 for label in invariants.final_live_labels if label.startswith("i")
    )
    assert len(fake_repo.pull_requests) == expected_pr_count
    _assert_no_transient_damage_events(
        baseline=baseline,
        expected_base_by_pr_number=expected_base_by_pr_number,
        fake_repo=fake_repo,
        invariants=invariants,
        strict_base_events=strict_base_events,
    )


def _assert_approval_review_preserved(
    fake_repo: FakeGithubRepository,
    pr_number: int,
    label: str,
) -> None:
    assert any(
        review.reviewer_login == f"reviewer-{label}" and review.state == "APPROVED"
        for review in fake_repo.list_pull_request_reviews(pr_number)
    )


def _assert_no_transient_damage_events(
    *,
    baseline: dict[str, SubmittedBaseline],
    expected_base_by_pr_number: dict[int, str],
    fake_repo: FakeGithubRepository,
    invariants: SubmitInvariants,
    strict_base_events: bool,
) -> None:
    original_pr_numbers = {submitted.pr_number for submitted in baseline.values()}
    orphan_pr_numbers = {baseline[label].pr_number for label in invariants.orphaned_labels}
    expected_changed_base_pr_numbers = {
        submitted.pr_number
        for submitted in baseline.values()
        if expected_base_by_pr_number.get(submitted.pr_number) != submitted.pr_base_ref
    }
    for event in fake_repo.pull_request_events:
        if event.pull_request_number not in original_pr_numbers:
            continue
        assert event.kind != "state", event
        if event.pull_request_number in orphan_pr_numbers:
            assert event.kind != "base", event
        if strict_base_events and event.kind == "base":
            assert event.pull_request_number in expected_changed_base_pr_numbers, event


def _assert_deferred_stack_untouched(
    *,
    baseline: dict[str, SubmittedBaseline],
    fake_repo: FakeGithubRepository,
    repo: Path,
    scenario: CrossStackSplitScenario,
) -> None:
    _assert_deferred_labels_untouched(
        baseline=baseline,
        deferred_labels=scenario.deferred_labels,
        fake_repo=fake_repo,
        repo=repo,
    )


def _assert_deferred_labels_untouched(
    *,
    baseline: dict[str, SubmittedBaseline],
    deferred_labels: tuple[str, ...],
    fake_repo: FakeGithubRepository,
    repo: Path,
) -> None:
    state = ReviewStateStore.for_repo(repo).load()
    remote_heads = _remote_refs(fake_repo.git_dir)
    deferred_pr_numbers = {
        baseline[label].pr_number for label in deferred_labels
    }
    for label in deferred_labels:
        submitted = baseline[label]
        cached_change = state.changes[submitted.change_id]
        pull_request = fake_repo.pull_requests[submitted.pr_number]
        assert cached_change == submitted.cached_change
        assert _remote_head(remote_heads, submitted.bookmark) == submitted.remote_target
        assert pull_request.base_ref == submitted.pr_base_ref
        assert pull_request.head_ref == submitted.bookmark
        assert pull_request.merged_at is None
        assert pull_request.state == "open"
        _assert_approval_review_preserved(fake_repo, submitted.pr_number, label)

    for event in fake_repo.pull_request_events:
        if event.pull_request_number in deferred_pr_numbers:
            assert event.kind != "base", event


def _assert_retry_metadata(fake_repo: FakeGithubRepository) -> None:
    for pull_request in fake_repo.pull_requests.values():
        assert "needs-review" in pull_request.labels
        assert "alice" in pull_request.requested_reviewers
        assert "platform" in pull_request.requested_team_reviewers


def _apply_drift_operation(
    *,
    baseline: dict[str, SubmittedBaseline],
    drift: DriftOperation,
    fake_repo: FakeGithubRepository,
    labels_to_change_ids: dict[str, str],
    repo: Path,
    run_cli: CliRunner,
) -> str | None:
    """Apply one external-actor transition; return a submit revset override if any."""

    if drift.kind == "trunk_advanced":
        advance_remote_trunk(fake_repo)
        return None

    label = drift.label
    assert label is not None, drift.trace
    submitted = baseline[label]

    if drift.kind == "closed_pr":
        fake_repo.update_pull_request_state(
            fake_repo.pull_requests[submitted.pr_number],
            state="closed",
            reason="external_close",
        )
        return None
    if drift.kind == "merged_pr":
        pull_request = fake_repo.pull_requests[submitted.pr_number]
        pull_request.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        fake_repo.update_pull_request_state(
            pull_request,
            state="closed",
            reason="external_merge",
        )
        return None
    if drift.kind == "pr_replaced":
        old_pull_request = fake_repo.pull_requests[submitted.pr_number]
        fake_repo.update_pull_request_state(
            old_pull_request,
            state="closed",
            reason="external_close",
        )
        fake_repo.create_pull_request(
            base_ref=old_pull_request.base_ref,
            body="recreated outside jj-stack",
            head_ref=old_pull_request.head_ref,
            title=f"recreated {subject_for_label(label)}",
        )
        return None
    if drift.kind == "pr_base_retargeted":
        fake_repo.update_pull_request_base(
            fake_repo.pull_requests[submitted.pr_number],
            base_ref="main",
            reason="external_retarget",
        )
        return None
    if drift.kind == "pr_draft_toggled":
        fake_repo.pull_requests[submitted.pr_number].is_draft = True
        return None
    if drift.kind == "wrong_saved_pr_number":
        state_store = ReviewStateStore.for_repo(repo)
        state = state_store.load()
        state_store.save(
            state.model_copy(
                update={
                    "changes": {
                        **state.changes,
                        submitted.change_id: state.changes[submitted.change_id].model_copy(
                            update={"pr_number": 999_999}
                        ),
                    }
                }
            )
        )
        return None
    if drift.kind == "unlinked_change":
        assert run_cli(("unlink", submitted.change_id)) == 0, drift.trace
        return None
    if drift.kind == "remote_branch_drift":
        drift_target = next(
            candidate.remote_target
            for candidate_label, candidate in reversed(baseline.items())
            if candidate_label != label
        )
        update_remote_ref(fake_repo, branch=submitted.bookmark, target=drift_target)
        return None
    if drift.kind == "remote_branch_deleted":
        # GitHub closes a pull request when its head branch is deleted, so the
        # faithful transition is branch deletion plus PR closure.
        run_command(
            [
                "git",
                "--git-dir",
                str(fake_repo.git_dir),
                "update-ref",
                "-d",
                f"refs/heads/{submitted.bookmark}",
            ],
            fake_repo.git_dir.parent,
        )
        fake_repo.update_pull_request_state(
            fake_repo.pull_requests[submitted.pr_number],
            state="closed",
            reason="head_branch_deleted",
        )
        return None
    if drift.kind == "foreign_branch_fetched":
        # A copy of the submitted commit arrives on the remote under a foreign
        # branch name (an agent or teammate pushed it), and the user fetches.
        # The untracked remote bookmark makes the commit immutable; if the
        # change was rewritten since submit, the resurrected predecessor makes
        # it divergent instead. Either way the stack stops being reviewable.
        update_remote_ref(
            fake_repo,
            branch=f"agent/copy-{label}",
            target=submitted.remote_target,
        )
        run_command(["jj", "git", "fetch", "--remote", "origin"], repo)
        return None
    if drift.kind == "conflicted_rebase":
        conflicted_label = initial_label(1)
        run_command(["jj", "new", "main"], repo)
        write_file(repo / filename_for_label(conflicted_label), "trunk conflicting edit\n")
        run_command(["jj", "commit", "-m", "trunk conflicting edit"], repo)
        run_command(["jj", "bookmark", "move", "main", "--to", "@-"], repo)
        run_command(["jj", "git", "push", "--remote", "origin", "--bookmark", "main"], repo)
        run_command(
            ["jj", "rebase", "-s", labels_to_change_ids[conflicted_label], "-d", "main"],
            repo,
        )
        return labels_to_change_ids[label]
    if drift.kind == "merge_commit":
        run_command(["jj", "new", "main"], repo)
        commit_file(repo, "side branch", "side-branch.txt")
        side_change_id = JjClient(repo).resolve_revision("@-").change_id
        run_command(["jj", "new", labels_to_change_ids[label], side_change_id], repo)
        commit_file(repo, "merge commit", "merge-commit.txt")
        return JjClient(repo).resolve_revision("@-").change_id
    if drift.kind == "agent_recreated_change":
        new_label = drift.new_label
        assert new_label is not None, drift.trace
        _recreate_change_outside_jj_stack(
            fake_repo=fake_repo,
            labels_to_change_ids=labels_to_change_ids,
            new_label=new_label,
            repo=repo,
            replaced=submitted,
        )
        return None
    raise AssertionError(f"unsupported drift kind: {drift.kind}")


def _recreate_change_outside_jj_stack(
    *,
    fake_repo: FakeGithubRepository,
    labels_to_change_ids: dict[str, str],
    new_label: str,
    repo: Path,
    replaced: SubmittedBaseline,
) -> None:
    """Replace a reviewed change's PR and branch with `gh`-style equivalents.

    The replacement local change must already exist (the scenario's edit
    operations abandon the original and insert the recreation). This applies
    the external half: close the original PR, delete its review branch, push
    the recreated commit with plain git, open a replacement PR, and fetch.
    """

    fake_repo.update_pull_request_state(
        fake_repo.pull_requests[replaced.pr_number],
        state="closed",
        reason="external_close",
    )
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{replaced.bookmark}",
        ],
        fake_repo.git_dir.parent,
    )
    recreated_commit_id = JjClient(repo).resolve_revision(
        labels_to_change_ids[new_label]
    ).commit_id
    recreated_branch = f"agent/recreated-{new_label}"
    run_command(
        [
            "git",
            "--git-dir",
            str(_jj_backing_git_dir(repo)),
            "push",
            str(fake_repo.git_dir),
            f"{recreated_commit_id}:refs/heads/{recreated_branch}",
        ],
        repo,
    )
    fake_repo.create_pull_request(
        base_ref="main",
        body="recreated with gh",
        head_ref=recreated_branch,
        title=subject_for_label(new_label),
    )
    run_command(["jj", "git", "fetch", "--remote", "origin"], repo)


def _jj_backing_git_dir(repo: Path) -> Path:
    store = repo / ".jj" / "repo" / "store"
    git_target = store / "git_target"
    if git_target.is_file():
        return (store / git_target.read_text(encoding="utf-8").strip()).resolve()
    return store / "git"


def advance_remote_trunk(fake_repo: FakeGithubRepository) -> None:
    """Land unrelated external work on the remote default branch."""

    git_dir = str(fake_repo.git_dir)
    cwd = fake_repo.git_dir.parent
    head = run_command(
        ["git", "--git-dir", git_dir, "rev-parse", "refs/heads/main"], cwd
    ).stdout.strip()
    tree = run_command(
        ["git", "--git-dir", git_dir, "rev-parse", "refs/heads/main^{tree}"], cwd
    ).stdout.strip()
    new_commit = run_command(
        [
            "git",
            "-c",
            "user.name=External User",
            "-c",
            "user.email=external@example.com",
            "--git-dir",
            git_dir,
            "commit-tree",
            tree,
            "-p",
            head,
            "-m",
            "external trunk commit",
        ],
        cwd,
    ).stdout.strip()
    update_remote_ref(fake_repo, branch="main", target=new_commit)


def update_remote_ref(fake_repo: FakeGithubRepository, *, branch: str, target: str) -> None:
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            f"refs/heads/{branch}",
            target,
        ],
        fake_repo.git_dir.parent,
    )


def _github_snapshot(
    fake_repo: FakeGithubRepository,
) -> tuple[object, ...]:
    """All observable fake-GitHub state a fail-closed submit must preserve."""

    pull_requests = {
        number: (
            pull_request.base_ref,
            pull_request.head_ref,
            pull_request.state,
            pull_request.merged_at or "",
            pull_request.title,
            pull_request.body,
            pull_request.is_draft,
            tuple(pull_request.labels),
            tuple(pull_request.requested_reviewers),
            tuple(pull_request.requested_team_reviewers),
        )
        for number, pull_request in fake_repo.pull_requests.items()
    }
    comments = {
        issue_number: tuple(
            (comment.id, comment.issue_number, comment.body) for comment in issue_comments
        )
        for issue_number, issue_comments in fake_repo.issue_comments.items()
    }
    reviews = {
        pull_number: tuple(
            (review.id, review.reviewer_login, review.state) for review in pull_reviews
        )
        for pull_number, pull_reviews in fake_repo.pull_request_reviews.items()
    }
    return (
        pull_requests,
        comments,
        reviews,
        fake_repo.next_issue_comment_id,
        fake_repo.next_pull_request_number,
        fake_repo.next_pull_request_review_id,
    )


def _bookmark_snapshot(repo: Path) -> dict[str, object]:
    """The complete local and remembered-remote bookmark view."""

    return dict(JjClient(repo).list_bookmark_states())


def _remote_head(remote_heads: dict[str, str], bookmark: str) -> str:
    return remote_heads[f"refs/heads/{bookmark}"]


def _remote_refs(remote: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref", "--heads"],
        capture_output=True,
        check=False,
        cwd=remote.parent,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise AssertionError(
            "['git', '--git-dir', "
            f"{str(remote)!r}, 'show-ref', '--heads'] failed:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    refs: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        commit_id, ref_name = line.split(" ", maxsplit=1)
        refs[ref_name] = commit_id
    return refs
