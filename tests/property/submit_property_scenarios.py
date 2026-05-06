from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.submit_command_helpers import configure_submit_environment, run_main
from tests.support.integration_helpers import init_fake_github_repo
from tests.support.submit_property_harness import (
    replay_boundary_drift_scenario,
    replay_cross_stack_split_scenario,
    replay_stack_merge_scenario,
    replay_stack_move_scenario,
    replay_successful_stack_edit_scenario,
)
from tests.support.submit_property_scenarios import (
    BoundaryDriftScenario,
    CrossStackSplitScenario,
    StackEditScenario,
    StackMergeScenario,
    StackMoveScenario,
    boundary_drift_scenarios,
    cross_stack_scenarios_from_environment,
    stack_edit_scenarios_from_environment,
    stack_merge_scenarios_from_environment,
    stack_move_scenarios_from_environment,
)

STACK_EDIT_SCENARIOS = stack_edit_scenarios_from_environment()
CROSS_STACK_SCENARIOS = cross_stack_scenarios_from_environment()
STACK_MERGE_SCENARIOS = stack_merge_scenarios_from_environment()
STACK_MOVE_SCENARIOS = stack_move_scenarios_from_environment()
BOUNDARY_DRIFT_SCENARIOS = boundary_drift_scenarios()


@pytest.mark.parametrize(
    "scenario",
    STACK_EDIT_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_stack_edits_preserve_review_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: StackEditScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    def submit(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(repo, config_path, "submit", *args)

    replay_successful_stack_edit_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


@pytest.mark.parametrize(
    "scenario",
    CROSS_STACK_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_cross_stack_split_updates_selected_stack_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: CrossStackSplitScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    def submit(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(repo, config_path, "submit", *args)

    replay_cross_stack_split_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


@pytest.mark.parametrize(
    "scenario",
    STACK_MERGE_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_stack_merge_preserves_review_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: StackMergeScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    def submit(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(repo, config_path, "submit", *args)

    replay_stack_merge_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


@pytest.mark.parametrize(
    "scenario",
    STACK_MOVE_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_stack_move_updates_selected_stack_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: StackMoveScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    def submit(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(repo, config_path, "submit", *args)

    replay_stack_move_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


@pytest.mark.parametrize(
    "scenario",
    BOUNDARY_DRIFT_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_boundary_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: BoundaryDriftScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    def submit(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(repo, config_path, "submit", *args)

    replay_boundary_drift_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )
