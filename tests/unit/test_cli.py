import re
from pathlib import Path

import pytest

import jj_stack.ui as ui
from jj_stack.cli import main
from jj_stack.commands.view import ViewSelector
from jj_stack.errors import EXIT_USAGE, CliError
from tests.support.output_assertions import assert_output_contains, assert_output_in_order


@pytest.fixture(autouse=True)
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jj_stack.cli._load_configured_jj_color", lambda **kwargs: None)


def test_main_reports_missing_repository_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "missing-repo"

    exit_code = main(["--repository", str(repository), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert str(repository) in captured.err
    assert "does not exist" in captured.err
    assert "Traceback" not in captured.err


def test_main_reports_invalid_logging_level_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _patch_fake_jj_workspace(
        monkeypatch,
        tmp_path,
        jj_stack_config_stdout='jj-stack.logging.level = "DEBIG"\n',
    )

    exit_code = main(["--repository", str(repo), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid logging level" in captured.err
    assert "DEBIG" in captured.err
    assert "Traceback" not in captured.err


def test_main_reports_non_jj_directory_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain_dir = tmp_path / "not-a-jj-repo"
    plain_dir.mkdir()

    exit_code = main(["--repository", str(plain_dir), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Not inside a jj workspace" in captured.err
    assert "Traceback" not in captured.err


def test_main_renders_semantic_cli_errors_without_flattening_first(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_view(**kwargs) -> int:
        raise CliError(("Problem at ", ui.change_id("abcdefgh1234")))

    monkeypatch.setattr("jj_stack.cli.view_command.view", fake_view)

    exit_code = main(["view"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error: Problem at abcdefgh" in captured.err


def test_main_renders_cli_error_hint_on_separate_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_view(**kwargs) -> int:
        raise CliError("Problem at trunk.", hint="Run view and retry.")

    monkeypatch.setattr("jj_stack.cli.view_command.view", fake_view)

    exit_code = main(["view"])
    captured = capsys.readouterr()

    assert exit_code == 1
    err_lines = captured.err.splitlines()
    assert err_lines[0] == "Error: Problem at trunk."
    assert "Hint: Run view and retry." in err_lines


def test_cleanup_close_requires_pull_request_selection(capsys) -> None:
    exit_code = main(["cleanup", "--close"])
    captured = capsys.readouterr()

    assert exit_code == EXIT_USAGE
    assert "cleanup --close requires --pull-request" in captured.err


def test_sync_help_hanging_indents_wrapped_bullets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "60")

    exit_code = main(["sync", "--help"])
    lines = capsys.readouterr().out.splitlines()

    assert exit_code == 0
    bullet_indexes = [index for index, line in enumerate(lines) if line.startswith("- ")]
    assert bullet_indexes
    for index in bullet_indexes:
        following = lines[index + 1 :]
        wrapped_lines = next(
            (following[:end] for end, line in enumerate(following) if not line),
            following,
        )
        assert wrapped_lines
        assert all(line.startswith("  ") for line in wrapped_lines)


def test_help_all_in_one_marks_cli_tokens_for_styling(capsys) -> None:
    exit_code = main(["help", "--all-in-one"])
    captured = capsys.readouterr()

    assert exit_code == 0
    for class_name in ("cli-command", "cli-option", "cli-metavar"):
        assert f'class="{class_name}"' in captured.out

    unmarked = re.sub(r"<code\b[^>]*>.*?</code>", "", captured.out, flags=re.DOTALL)
    unmarked = re.sub(r"<span\b[^>]*>.*?</span>", "", unmarked, flags=re.DOTALL)
    assert re.search(r"(?<![\w-])--[a-z]", unmarked) is None
    assert re.search(r'<code class="cli-inline">@-?</code>', captured.out) is None
    assert re.search(r'<code class="cli-inline">[^<]*--', captured.out) is None


def test_help_all_in_one_groups_detailed_commands(capsys) -> None:
    exit_code = main(["help", "--all-in-one"])
    detailed_commands = capsys.readouterr().out.split("## Commands", 1)[1]

    assert exit_code == 0
    assert_output_in_order(
        detailed_commands,
        "### Core commands",
        "#### submit",
        "### Support commands",
        "#### cleanup",
        "### Advanced repair",
        "#### relink",
        "### Configuration",
        "#### completion",
        "### Help",
        "#### help",
    )


def test_help_all_in_one_lists_global_options_once(capsys) -> None:
    exit_code = main(["help", "--all-in-one"])
    output = capsys.readouterr().out
    global_options, detailed_commands = output.split("## Global options", 1)[1].split(
        "## Commands", 1
    )
    option_pattern = r'<span class="cli-option">([^<]+)</span>'

    assert exit_code == 0
    for option in re.findall(option_pattern, global_options):
        assert f'<span class="cli-option">{option}</span>' not in detailed_commands


def test_help_all_in_one_synopses_prefer_long_option_names(capsys) -> None:
    exit_code = main(["help", "--all-in-one"])
    output = capsys.readouterr().out
    short_aliases = re.findall(
        r'<span class="cli-option">(-[^-<][^<]*)</span>, '
        r'<span class="cli-option">(--[^<]+)</span>',
        output,
    )
    synopses = "\n".join(re.findall(r'<pre class="cli-synopsis">.*?</pre>', output))

    assert exit_code == 0
    assert short_aliases
    for short_option, _ in short_aliases:
        assert f'<span class="cli-option">{short_option}</span>' not in synopses


def test_help_all_keeps_terminal_top_level_contract(capsys) -> None:
    exit_code = main(["help", "--all"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert_output_contains(captured.out, "Usage: jj-stack", "Advanced repair:")
    assert "Generated by jj-stack" not in captured.out
    assert "Usage: jj-stack submit" not in captured.out


@pytest.mark.parametrize("command", ["view", "status", "st", "v"])
def test_main_preserves_view_selector_order(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    observed: dict[str, object] = {}

    def fake_view(**kwargs) -> int:
        observed.update(kwargs)
        return 0

    monkeypatch.setattr("jj_stack.cli.view_command.view", fake_view)

    exit_code = main([command, "foo", "--pull-request", "17", "bar"])

    assert exit_code == 0
    assert observed["selectors"] == (
        ViewSelector(kind="revset", value="foo"),
        ViewSelector(kind="pull_request", value="17"),
        ViewSelector(kind="revset", value="bar"),
    )


@pytest.mark.parametrize(
    ("argv", "expected_revsets"),
    [
        (["view", "--", "--pull-request", "7"], ["--pull-request", "7"]),
        (["view", "foo", "--", "-f"], ["foo", "-f"]),
    ],
)
def test_main_preserves_view_positional_escape_for_dash_prefixed_revsets(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_revsets: list[str],
) -> None:
    observed: dict[str, object] = {}

    def fake_view(**kwargs) -> int:
        observed.update(kwargs)
        return 0

    monkeypatch.setattr("jj_stack.cli.view_command.view", fake_view)

    exit_code = main(argv)

    assert exit_code == 0
    assert observed["revset"] == expected_revsets
    assert observed["selectors"] == tuple(
        ViewSelector(kind="revset", value=value) for value in expected_revsets
    )


@pytest.mark.parametrize("argv", [["pants"], ["pants", "-h"], ["help", "pants"]])
def test_main_reports_unknown_command_with_short_recovery_hint(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(argv)
    captured = capsys.readouterr()

    assert exit_code == EXIT_USAGE
    assert_output_contains(captured.err, "Unknown command pants.")
    err_lines = captured.err.splitlines()
    assert err_lines[0] == "Error: Unknown command pants."
    assert "Hint: Run jj-stack help to list commands." in err_lines


@pytest.mark.merge_recovery
def test_sync_all_rejects_a_revision_before_repository_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--repository", str(tmp_path), "sync", "--all", "some-selector"])
    captured = capsys.readouterr()

    assert exit_code == EXIT_USAGE
    assert "Use either jj-stack sync --all or a revision, not both." in captured.err


def _patch_fake_jj_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    jj_stack_config_stdout: str,
) -> Path:
    """Create a minimal .jj-marked directory and stub out the jj config read.

    Lets unit tests reach the jj-stack config validation path without
    requiring a real jj workspace or subprocess call.
    """

    repo = tmp_path / "repo"
    (repo / ".jj").mkdir(parents=True)
    monkeypatch.setattr(
        "jj_stack.jj.client.JjClient.read_jj_stack_config_list_output",
        lambda self: jj_stack_config_stdout,
    )
    return repo
