from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.submit_command_helpers import configure_submit_environment, run_main
from tests.support.integration_helpers import init_fake_github_repo_with_submitted_stack
from tests.support.land_property_harness import replay_land_scenario
from tests.support.land_property_scenarios import (
    LandScenario,
    land_scenarios_from_environment,
)

LAND_SCENARIOS = land_scenarios_from_environment()


@pytest.mark.parametrize(
    "scenario",
    LAND_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_land_property_model_matches_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: LandScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(
        tmp_path, size=scenario.initial_size
    )
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    def run_cli(args: tuple[str, ...]) -> int:
        return run_main(repo, config_path, *args)

    replay_land_scenario(
        fake_repo=fake_repo,
        read_output=capsys.readouterr,
        repo=repo,
        run_cli=run_cli,
        scenario=scenario,
    )
