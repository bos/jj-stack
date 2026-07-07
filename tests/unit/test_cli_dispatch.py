from pathlib import Path

import pytest

import jj_stack.cli as cli_module
from jj_stack.cli import _extract_config_overrides, main


@pytest.fixture(autouse=True)
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_load_configured_jj_color", lambda **kwargs: None)


def test_main_reports_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_module.view_command,
        "view",
        lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    exit_code = main(["view"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out.strip() == ""
    assert captured.err.strip() == "Interrupted."
    assert "Traceback" not in captured.err


def test_main_preserves_partial_handler_output_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_view(**kwargs) -> int:
        print("before interrupt")
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_module.view_command, "view", fake_view)

    exit_code = main(["view"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert "before interrupt" in captured.out
    assert captured.err.strip() == "Interrupted."
    assert "Traceback" not in captured.err


def test_delete_alias_dispatches_to_the_unstack_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`delete` must stay registered as an argparse alias of `unstack`."""

    calls: list[str] = []

    def fake_unstack(**kwargs) -> int:
        calls.append("unstack")
        return 0

    monkeypatch.setattr(cli_module.unstack_command, "unstack", fake_unstack)

    exit_code = main(["delete"])

    assert exit_code == 0
    assert calls == ["unstack"]


def test_config_overrides_preserve_argv_order_across_the_subcommand(
    tmp_path: Path,
) -> None:
    """Overrides before and after the subcommand must all be retained in argv order.

    Regression: argparse subparsers dispatch into a fresh namespace and copy
    it back, which used to drop any ``--config`` / ``--config-file`` passed
    before the subcommand when the subcommand also carried its own.
    """

    file_a = tmp_path / "a.toml"
    file_a.write_text("", encoding="utf-8")
    cli_args, remaining = _extract_config_overrides(
        [
            "--config-file",
            str(file_a),
            "view",
            "--config",
            "revset-aliases.myhead=@-",
            "--repository",
            ".",
        ]
    )

    assert cli_args.to_argv() == (
        "--config-file",
        str(file_a),
        "--config",
        "revset-aliases.myhead=@-",
    )
    assert remaining == ["view", "--repository", "."]


def test_config_file_paths_resolve_against_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_cwd = tmp_path / "work"
    caller_cwd.mkdir()
    config_file = caller_cwd / "jjr.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.chdir(caller_cwd)

    cli_args, _ = _extract_config_overrides(["--config-file", "jjr.toml", "view"])

    assert cli_args.to_argv() == ("--config-file", str(config_file.resolve()))


def test_config_overrides_leave_malformed_flag_for_argparse_to_report() -> None:
    """When the next token is another option, ``--config`` stays in argv so
    argparse raises its usual "expected one argument" error instead of silently
    eating the option as the value.
    """

    cli_args, remaining = _extract_config_overrides(
        ["--config", "--repository", ".", "view"]
    )

    assert cli_args.to_argv() == ()
    assert remaining == ["--config", "--repository", ".", "view"]


def test_config_overrides_stop_at_end_of_options_marker() -> None:
    """Tokens after ``--`` are positional and must not be pulled out as overrides."""

    cli_args, remaining = _extract_config_overrides(
        ["view", "--", "--config", "x=1"]
    )

    assert cli_args.to_argv() == ()
    assert remaining == ["view", "--", "--config", "x=1"]

