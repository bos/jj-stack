from tests import run_submit_property_scenarios as property_runner


def test_reproduction_command_preserves_scenario_budgets_and_pytest_filter() -> None:
    env = {
        environment_name: str(index)
        for index, (_option, environment_name) in enumerate(
            property_runner._REPRODUCTION_SCENARIO_OPTIONS,
            start=1,
        )
    }

    command = property_runner._build_reproduction_command(
        env=env,
        jobs="auto",
        no_sync=True,
        pytest_args=("-k", "external_drift"),
        scenarios=10,
        seed=424242,
    )

    assert command[:6] == (
        "tests/run_submit_property_scenarios.py",
        "10",
        "--seed",
        "424242",
        "--jobs",
        "auto",
    )
    for option, environment_name in property_runner._REPRODUCTION_SCENARIO_OPTIONS:
        option_index = command.index(option)
        assert command[option_index + 1] == env[environment_name]
    assert command[-4:] == ("--no-sync", "--", "-k", "external_drift")
