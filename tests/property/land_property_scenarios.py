from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.submit_command_helpers import configure_submit_environment, run_main
from tests.support.integration_helpers import init_fake_github_repo_with_submitted_stack
from tests.support.land_property_harness import (
    replay_land_drift_scenario,
    replay_land_scenario,
)
from tests.support.land_property_scenarios import (
    LandDriftScenario,
    LandScenario,
    land_drift_scenarios_from_environment,
    land_scenarios_from_environment,
)

import jj_stack.cli as cli_module
from jj_stack.errors import CliError

LAND_SCENARIOS = land_scenarios_from_environment()
LAND_DRIFT_SCENARIOS = land_drift_scenarios_from_environment()


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


@pytest.mark.parametrize(
    "scenario",
    LAND_DRIFT_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_land_property_external_drift_matches_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: LandDriftScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(
        tmp_path, size=scenario.initial_size
    )
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    # `main` swallows CliError into an exit code, so record the error it hands
    # to the top-level printer; the replay asserts the fail-closed shape.
    last_error: list[CliError | None] = [None]
    print_cli_error = cli_module._print_cli_error

    def print_and_record_cli_error(error: CliError) -> None:
        last_error[0] = error
        print_cli_error(error)

    monkeypatch.setattr(cli_module, "_print_cli_error", print_and_record_cli_error)

    def run_cli(args: tuple[str, ...]) -> int:
        last_error[0] = None
        return run_main(repo, config_path, *args)

    replay_land_drift_scenario(
        fake_repo=fake_repo,
        last_cli_error=lambda: last_error[0],
        read_output=capsys.readouterr,
        repo=repo,
        run_cli=run_cli,
        scenario=scenario,
    )
