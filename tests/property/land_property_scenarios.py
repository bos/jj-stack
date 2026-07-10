from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.submit_command_helpers import (
    configure_submit_environment,
    patch_github_client_builders,
    run_main,
)
from tests.support.fake_github import FakeGithubState, create_app
from tests.support.integration_helpers import init_fake_github_repo_with_submitted_stack
from tests.support.land_property_harness import (
    replay_land_drift_scenario,
    replay_land_handoff_scenario,
    replay_land_retry_scenario,
    replay_land_scenario,
)
from tests.support.land_property_scenarios import (
    LandDriftScenario,
    LandHandoffScenario,
    LandRetryScenario,
    LandScenario,
    land_drift_scenarios_from_environment,
    land_handoff_scenarios_from_environment,
    land_retry_scenarios_from_environment,
    land_scenarios_from_environment,
)

import jj_stack.cli as cli_module
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient

LAND_SCENARIOS = land_scenarios_from_environment()
LAND_DRIFT_SCENARIOS = land_drift_scenarios_from_environment()
LAND_RETRY_SCENARIOS = land_retry_scenarios_from_environment()
LAND_HANDOFF_SCENARIOS = land_handoff_scenarios_from_environment()


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


@pytest.mark.parametrize(
    "scenario",
    LAND_RETRY_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_land_property_interrupted_land_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: LandRetryScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(
        tmp_path, size=scenario.initial_size
    )
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    app = create_app(FakeGithubState.single_repository(fake_repo))
    fault_pull_number = scenario.fault_pull_number
    original_push_bookmarks = JjClient.push_bookmarks

    class FaultOnFinalizeLoadClient(GithubClient):
        async def get_pull_request(self, *, pull_number):
            if pull_number == fault_pull_number:
                raise GithubClientError(
                    "Simulated finalization failure", status_code=500
                )
            return await super().get_pull_request(pull_number=pull_number)

    def install_fault() -> None:
        if scenario.fault == "after_push_ack_lost":
            failed = False

            def push_then_lose_ack(self, *, remote, bookmarks) -> None:
                nonlocal failed
                original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)
                if bookmarks == ("main",) and not failed:
                    failed = True
                    raise CliError("Simulated lost trunk push acknowledgement")

            monkeypatch.setattr(JjClient, "push_bookmarks", push_then_lose_ack)
            return
        patch_github_client_builders(
            monkeypatch,
            app=app,
            fake_repo=fake_repo,
            modules=("jj_stack.commands.land.command",),
            client_type=FaultOnFinalizeLoadClient,
        )

    def restore_github() -> None:
        monkeypatch.setattr(JjClient, "push_bookmarks", original_push_bookmarks)
        patch_github_client_builders(
            monkeypatch,
            app=app,
            fake_repo=fake_repo,
            modules=("jj_stack.commands.land.command",),
        )

    def run_cli(args: tuple[str, ...]) -> int:
        return run_main(repo, config_path, *args)

    replay_land_retry_scenario(
        fake_repo=fake_repo,
        install_fault=install_fault,
        read_output=capsys.readouterr,
        repo=repo,
        restore_github=restore_github,
        run_cli=run_cli,
        scenario=scenario,
    )


@pytest.mark.parametrize(
    "scenario",
    LAND_HANDOFF_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_land_property_merged_prefix_handoff_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: LandHandoffScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(
        tmp_path, size=scenario.initial_size
    )
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    app = create_app(FakeGithubState.single_repository(fake_repo))
    fault_pull_number = scenario.fault_pull_number

    class FaultOnMergeClient(GithubClient):
        async def merge_pull_request(self, *, pull_number: int, merge_method: str) -> None:
            if pull_number == fault_pull_number:
                raise GithubClientError("Simulated merge failure", status_code=500)
            await super().merge_pull_request(
                pull_number=pull_number, merge_method=merge_method
            )

    def install_fault() -> None:
        patch_github_client_builders(
            monkeypatch,
            app=app,
            fake_repo=fake_repo,
            modules=("jj_stack.commands.land.command",),
            client_type=FaultOnMergeClient,
        )

    def restore_github() -> None:
        patch_github_client_builders(
            monkeypatch,
            app=app,
            fake_repo=fake_repo,
            modules=("jj_stack.commands.land.command",),
        )

    def run_cli(args: tuple[str, ...]) -> int:
        return run_main(repo, config_path, *args)

    replay_land_handoff_scenario(
        fake_repo=fake_repo,
        install_fault=install_fault,
        read_output=capsys.readouterr,
        repo=repo,
        restore_github=restore_github,
        run_cli=run_cli,
        scenario=scenario,
    )
