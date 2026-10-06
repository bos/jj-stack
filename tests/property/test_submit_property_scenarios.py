"""Fixed regressions and generated sequences share the same actions and assertions."""

import json
import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import seed, settings
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.stateful import run_state_machine_as_test
from tests.support.stack_edit_scenarios import StackEditOperation
from tests.support.stack_machine import RULE_NAMES, StackMachine

SEARCH_EXAMPLES = os.environ.get("JJ_STACK_PROPERTY_EXAMPLES")
SHARDS = int(os.environ.get("JJ_STACK_PROPERTY_SHARDS", "1"))


@pytest.fixture
def machine() -> Iterator[StackMachine]:
    machine = StackMachine()
    try:
        yield machine
        machine.model_matches()
    finally:
        machine.teardown()


def test_squashing_a_submitted_middle_change_preserves_its_orphan_pr(
    machine: StackMachine,
) -> None:
    machine.start(size=3, submitted=True)
    machine.apply_edit(0, StackEditOperation("squash_into_previous", "c2"))
    machine.submit_path(0)


def test_joining_submitted_stacks_preserves_both_sets_of_prs(machine: StackMachine) -> None:
    machine.start(size=2, submitted=True)
    machine.new_stack(2)
    machine.submit_path(1)
    machine.approve(machine.paths[1])
    machine.join_paths(1, 0)
    machine.submit_path(0)


def test_submit_recovers_after_an_unacknowledged_push(machine: StackMachine) -> None:
    machine.start(size=3, submitted=False)
    machine.interrupted_submit(0, "after_remote_push", 0)
    machine.apply_edit(0, StackEditOperation("rewrite", "c2"))
    machine.drift("trunk_advanced")
    machine.submit_path(0)


def test_a_closed_pr_blocks_publishing_an_inserted_change(machine: StackMachine) -> None:
    machine.start(size=3, submitted=True)
    machine.apply_edit(0, StackEditOperation("insert_after", "c1", new_label="c4"))
    machine.drift("closed_pr", "c2")
    machine.submit_path(0)


def test_cleanup_requires_sync_after_an_external_squash_merge(machine: StackMachine) -> None:
    machine.start(size=1, submitted=True)
    machine.server_merge(0, 1, "squash")
    machine.cleanup_before_sync(0)
    machine.sync_path(0)


def test_partial_rebase_merge_preserves_surviving_ids_and_reviews(machine: StackMachine) -> None:
    machine.start(size=4, submitted=True)
    machine.apply_edit(0, StackEditOperation("insert_after", "c4", new_label="c5"))
    machine.server_merge(0, 2, "rebase")
    machine.apply_edit(0, StackEditOperation("rewrite", "c5"))
    machine.sync_path(0)


def test_sync_replaces_survivors_github_rewrote_after_trunk_advanced(
    machine: StackMachine,
) -> None:
    machine.start(size=3, submitted=True)
    machine.server_merge(0, 1, "squash", rewrite=False)
    machine.drift("trunk_advanced")
    assert machine.fake.rewrite_pending_survivors()
    machine.sync_path(0)


def test_sync_all_finishes_a_merged_pr_left_by_an_interrupted_sync(
    machine: StackMachine,
) -> None:
    machine.start(size=2, submitted=True)
    machine.server_merge(0, 1, "squash")
    machine.interrupted_sync(0)
    machine.sync_all_paths()


def test_sync_all_does_not_report_the_pr_of_a_stack_it_syncs(machine: StackMachine) -> None:
    machine.start(size=2, submitted=True, merged=True)
    # Local and trunk edits to the merged change's file leave the survivor conflicted, so the
    # first sync stops before updating its PR, which GitHub has rewritten meanwhile.
    machine.shared_edit(0, server=False)
    machine.shared_edit(0, server=True)
    machine.sync_all_paths()
    machine.sync_all_paths()


def test_waiting_for_another_stack_completes_queued_prs(machine: StackMachine) -> None:
    machine.start(size=3, submitted=True, queue=True)
    machine.enqueue_path(0, 2)
    machine.new_stack(1)
    machine.submit_path(1)
    machine.approve(machine.paths[1])
    machine.wait_for_queue(1, 1, None)
    assert machine.merged(machine.paths[0]) == ("c1", "c2")


@pytest.mark.skipif(
    SEARCH_EXAMPLES is None, reason="generated sequences run through `just property`"
)
@pytest.mark.parametrize("shard", range(SHARDS))
def test_generated_commands(shard: int) -> None:
    machines: list[StackMachine] = []

    @seed(int(os.environ["JJ_STACK_PROPERTY_SEED"]) + shard)
    def factory() -> StackMachine:
        machines.append(StackMachine())
        return machines[-1]

    try:
        run_state_machine_as_test(
            factory,
            settings=settings(
                max_examples=int(os.environ["JJ_STACK_PROPERTY_EXAMPLES"]),
                stateful_step_count=int(os.environ["JJ_STACK_PROPERTY_STEPS"]),
                deadline=None,
                database=DirectoryBasedExampleDatabase(f".hypothesis/examples/{shard}"),
            ),
        )
    finally:
        report_reach(shard, machines)


def report_reach(shard: int, machines: list[StackMachine]) -> None:
    """Record which rules this shard's sequences fired, for `just property` to sum up."""

    report_dir = os.environ.get("JJ_STACK_PROPERTY_REPORT_DIR")
    if report_dir is None:
        return
    fired = Counter(name for machine in machines for name in machine.fired)
    Path(report_dir, f"shard-{shard}.json").write_text(
        json.dumps({"sequences": len(machines), "rules": sorted(RULE_NAMES), "fired": fired})
    )
