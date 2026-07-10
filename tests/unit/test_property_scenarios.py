import pytest

from tests.support.stack_edit_scenarios import StackEditOperation, apply_stack_edit
from tests.support.submit_property_scenarios import (
    DriftOperation,
    ExternalDriftScenario,
)


@pytest.mark.parametrize(
    ("operation", "expected_labels"),
    [
        (StackEditOperation(kind="abandon", label="c2"), ("c1", "c3")),
        (
            StackEditOperation(kind="insert_after", label="c1", new_label="i1"),
            ("c1", "i1", "c2", "c3"),
        ),
        (
            StackEditOperation(kind="insert_before", label="c2", new_label="i1"),
            ("c1", "i1", "c2", "c3"),
        ),
        (
            StackEditOperation(kind="move_after", label="c1", target_label="c2"),
            ("c2", "c1", "c3"),
        ),
        (
            StackEditOperation(kind="move_before", label="c3", target_label="c2"),
            ("c1", "c3", "c2"),
        ),
        (StackEditOperation(kind="move_to_top", label="c1"), ("c2", "c3", "c1")),
        (StackEditOperation(kind="rewrite", label="c2"), ("c1", "c2", "c3")),
        (
            StackEditOperation(kind="squash_into_previous", label="c2"),
            ("c1", "c3"),
        ),
    ],
)
def test_shared_stack_edit_vocabulary_models_every_operation(
    operation: StackEditOperation,
    expected_labels: tuple[str, ...],
) -> None:
    effect = apply_stack_edit(("c1", "c2", "c3"), operation)

    assert effect.live_labels == expected_labels


def test_composed_drift_keeps_exit_codes_paired_with_their_diagnoses() -> None:
    scenario = ExternalDriftScenario(
        name="mixed-failures",
        hazard_class="unit",
        initial_size=2,
        edit_operations=(),
        drifts=(
            DriftOperation(kind="closed_pr", label="c1"),
            DriftOperation(kind="foreign_branch_fetched", label="c2"),
        ),
        final_live_labels=("c1", "c2"),
        orphaned_labels=(),
        rewritten_initial_labels=(),
    )

    assert scenario.expected_failures == (
        (1, "pull_request_not_open"),
        (2, "unsupported_stack:divergent_change"),
        (2, "unsupported_stack:immutable_commit"),
    )
