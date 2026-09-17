import pytest

from tests import run_submit_property_scenarios as runner


def test_reproduction_preserves_search_budgets_seed_and_pytest_filter(
    monkeypatch, capsys
) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return runner.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner.os, "process_cpu_count", lambda: 14)
    assert (
        runner.main(
            (
                "10",
                "--steps",
                "30",
                "--shards",
                "2",
                "--seed",
                "424242",
                "--no-sync",
                "--",
                "-k",
                "generated_commands",
            )
        )
        == 0
    )
    command, env = calls[0]
    assert command[command.index("-n") + 1] == "14"
    assert command[-2:] == ["-k", "generated_commands"]
    assert env["JJ_STACK_PROPERTY_EXAMPLES"] == "10"
    assert env["JJ_STACK_PROPERTY_STEPS"] == "30"
    assert env["JJ_STACK_PROPERTY_SHARDS"] == "2"
    assert env["JJ_STACK_PROPERTY_SEED"] == "424242"
    assert capsys.readouterr().out.strip() == (
        "Reproduce: just property 10 --steps 30 --shards 2 --seed 424242 -n 14 "
        "-- -k generated_commands"
    )


def test_runner_requires_separator_before_pytest_arguments() -> None:
    with pytest.raises(SystemExit):
        runner.main(("1", "--no-sync", "-k", "generated_commands"))
