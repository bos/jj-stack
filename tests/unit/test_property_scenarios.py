from tests.support.submit_property_scenarios import (
    DriftOperation,
    ExternalDriftScenario,
)


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
