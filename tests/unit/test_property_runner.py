from subprocess import CompletedProcess

from tests import run_submit_property_scenarios as property_runner


def test_random_seed_is_printed_and_forwarded_to_scenarios_and_pytest(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[list[str] | tuple[str, ...], dict[str, str]]] = []

    def run(command, *, cwd, env):
        del cwd
        calls.append((command, env))
        return CompletedProcess(command, 0)

    monkeypatch.setattr(property_runner.secrets, "randbits", lambda _bits: 424242)
    monkeypatch.setattr(property_runner.subprocess, "run", run)

    exit_code = property_runner.main(["1", "--random-seed", "--no-sync", "-n", "1"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "property seed: 424242 (reproduce with --seed 424242)" in captured.out
    assert len(calls) == 1
    command, env = calls[0]
    assert "--randomly-seed=424242" in command
    assert env["JJ_STACK_SUBMIT_PROPERTY_SEED"] == "424242"
    assert env["JJ_STACK_LAND_PROPERTY_SEED"] == "424242"
