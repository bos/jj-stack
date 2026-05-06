from __future__ import annotations

from pathlib import Path

import pytest

from ..support.integration_helpers import init_fake_github_repo
from ..support.submit_property_harness import (
    replay_boundary_drift_scenario,
    replay_successful_stack_edit_scenario,
)
from ..support.submit_property_scenarios import (
    BoundaryDriftScenario,
    StackEditScenario,
    boundary_drift_scenarios,
    stack_edit_scenarios_from_environment,
)
from .submit_command_helpers import configure_submit_environment, run_main

STACK_EDIT_SCENARIOS = stack_edit_scenarios_from_environment()
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
