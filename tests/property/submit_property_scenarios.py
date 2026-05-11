from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.submit_command_helpers import (
    configure_submit_environment,
    patch_github_client_builders,
    run_main,
)
from tests.support.fake_github import FakeGithubState, create_app
from tests.support.integration_helpers import init_fake_github_repo
from tests.support.submit_property_harness import (
    replay_boundary_drift_scenario,
    replay_cross_stack_split_scenario,
    replay_failed_submit_retry_scenario,
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
    SubmitRetryScenario,
    boundary_drift_scenarios,
    cross_stack_scenarios_from_environment,
    stack_edit_scenarios_from_environment,
    stack_merge_scenarios_from_environment,
    stack_move_scenarios_from_environment,
    subject_for_label,
    submit_retry_scenarios_from_environment,
)

from jj_review.commands.submit import command as submit_command
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError

STACK_EDIT_SCENARIOS = stack_edit_scenarios_from_environment()
CROSS_STACK_SCENARIOS = cross_stack_scenarios_from_environment()
STACK_MERGE_SCENARIOS = stack_merge_scenarios_from_environment()
STACK_MOVE_SCENARIOS = stack_move_scenarios_from_environment()
SUBMIT_RETRY_SCENARIOS = submit_retry_scenarios_from_environment()
BOUNDARY_DRIFT_SCENARIOS = boundary_drift_scenarios()
RETRY_CONFIG_LINES = [
    'labels = ["needs-review"]',
    'reviewers = ["alice"]',
    'team_reviewers = ["platform"]',
]


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


@pytest.mark.parametrize(
    "scenario",
    SUBMIT_RETRY_SCENARIOS,
    ids=lambda scenario: scenario.name,
)
def test_submit_property_failed_submit_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: SubmitRetryScenario,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(
        monkeypatch,
        tmp_path,
        fake_repo,
        extra_config_lines=RETRY_CONFIG_LINES,
    )
    _install_submit_retry_fault(
        fake_repo=fake_repo,
        monkeypatch=monkeypatch,
        scenario=scenario,
    )

    def submit(revset: str | None) -> int:
        args = () if revset is None else (revset,)
        return run_main(repo, config_path, "submit", *args)

    replay_failed_submit_retry_scenario(
        discard_output=capsys.readouterr,
        fake_repo=fake_repo,
        repo=repo,
        scenario=scenario,
        submit=submit,
    )


def _install_submit_retry_fault(
    *,
    fake_repo,
    monkeypatch: pytest.MonkeyPatch,
    scenario: SubmitRetryScenario,
) -> None:
    if scenario.failure_point == "after_remote_push":
        _install_remote_push_fault(monkeypatch)
        return

    app = create_app(FakeGithubState.single_repository(fake_repo))
    failed = False
    target_title = subject_for_label(scenario.failure_label)

    class FaultingGithubClient(GithubClient):
        async def create_pull_request(self, owner, repo, *, base, body, draft=False, head, title):
            nonlocal failed
            pull_request = await super().create_pull_request(
                owner,
                repo,
                base=base,
                body=body,
                draft=draft,
                head=head,
                title=title,
            )
            if (
                not failed
                and scenario.failure_point == "create_pull_request"
                and title == target_title
            ):
                failed = True
                raise GithubClientError(
                    "Simulated pull request creation failure",
                    status_code=500,
                )
            return pull_request

        async def update_pull_request(self, owner, repo, *, pull_number, base, body, title):
            nonlocal failed
            pull_request = await super().update_pull_request(
                owner,
                repo,
                pull_number=pull_number,
                base=base,
                body=body,
                title=title,
            )
            if (
                not failed
                and scenario.failure_point == "update_pull_request"
                and title == target_title
            ):
                failed = True
                raise GithubClientError("Simulated pull request update failure", status_code=500)
            return pull_request

        async def add_labels(self, owner, repo, *, issue_number, labels):
            nonlocal failed
            await super().add_labels(
                owner,
                repo,
                issue_number=issue_number,
                labels=labels,
            )
            if (
                not failed
                and scenario.failure_point == "pull_request_metadata"
                and fake_repo.pull_requests[issue_number].title == target_title
            ):
                failed = True
                raise GithubClientError("Simulated label sync failure", status_code=500)

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        modules=("jj_review.commands.submit.command",),
        client_type=FaultingGithubClient,
    )


def _install_remote_push_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = False
    original_push_bookmarks = submit_command.JjClient.push_bookmarks

    def push_bookmarks_then_fail(self, *, remote, bookmarks) -> None:
        nonlocal failed
        original_push_bookmarks(self, remote=remote, bookmarks=bookmarks)
        if not failed:
            failed = True
            raise CliError("Simulated failure after remote branch push")

    monkeypatch.setattr(
        submit_command.JjClient,
        "push_bookmarks",
        push_bookmarks_then_fail,
    )
