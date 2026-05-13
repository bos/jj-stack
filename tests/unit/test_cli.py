from pathlib import Path

import pytest

import jj_review.ui as ui
from jj_review.cli import main
from jj_review.commands.status import StatusSelector
from jj_review.errors import CliError
from tests.support.output_assertions import assert_output_contains


@pytest.fixture(autouse=True)
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jj_review.cli._load_configured_jj_color", lambda **kwargs: None)


def test_main_reports_invalid_config_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _patch_fake_jj_workspace(
        monkeypatch,
        tmp_path,
        jj_review_config_stdout='jj-review.bookmark_prefix = ""\n',
    )

    exit_code = main(["--repository", str(repo), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.startswith("Error: ")
    assert "Invalid jj-review config" in captured.err
    assert "Traceback" not in captured.err


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
        jj_review_config_stdout='jj-review.logging.level = "DEBIG"\n',
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
    def fake_status(**kwargs) -> int:
        raise CliError(("Problem at ", ui.change_id("abcdefgh1234")))

    monkeypatch.setattr("jj_review.cli.status_command.status", fake_status)

    exit_code = main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error: Problem at abcdefgh" in captured.err


def test_main_renders_cli_error_hint_on_separate_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_status(**kwargs) -> int:
        raise CliError("Problem at trunk.", hint="Run status --fetch and retry.")

    monkeypatch.setattr("jj_review.cli.status_command.status", fake_status)

    exit_code = main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 1
    err_lines = captured.err.splitlines()
    assert err_lines[0] == "Error: Problem at trunk."
    assert "Hint: Run status --fetch and retry." in err_lines


def test_main_preserves_status_selector_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_status(**kwargs) -> int:
        observed.update(kwargs)
        return 0

    monkeypatch.setattr("jj_review.cli.status_command.status", fake_status)

    exit_code = main(["status", "foo", "--pull-request", "17", "bar"])

    assert exit_code == 0
    assert observed["selectors"] == (
        StatusSelector(kind="revset", value="foo"),
        StatusSelector(kind="pull_request", value="17"),
        StatusSelector(kind="revset", value="bar"),
    )


@pytest.mark.parametrize(
    ("argv", "expected_revsets"),
    [
        (["status", "--", "--pull-request", "7"], ["--pull-request", "7"]),
        (["status", "foo", "--", "-f"], ["foo", "-f"]),
    ],
)
def test_main_preserves_status_positional_escape_for_dash_prefixed_revsets(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_revsets: list[str],
) -> None:
    observed: dict[str, object] = {}

    def fake_status(**kwargs) -> int:
        observed.update(kwargs)
        return 0

    monkeypatch.setattr("jj_review.cli.status_command.status", fake_status)

    exit_code = main(argv)

    assert exit_code == 0
    assert observed["revset"] == expected_revsets
    assert observed["selectors"] == tuple(
        StatusSelector(kind="revset", value=value) for value in expected_revsets
    )


@pytest.mark.parametrize("argv", [["pants"], ["pants", "-h"], ["help", "pants"]])
def test_main_reports_unknown_command_with_short_recovery_hint(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(argv)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert_output_contains(captured.err, "Unknown command pants.")
    err_lines = captured.err.splitlines()
    assert err_lines[0] == "Error: Unknown command pants."
    assert "Hint: Run jj-review help to list commands." in err_lines


def _patch_fake_jj_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    jj_review_config_stdout: str,
) -> Path:
    """Create a minimal .jj-marked directory and stub out the jj config read.

    Lets unit tests reach the jj-review config validation path without
    requiring a real jj workspace or subprocess call.
    """

    repo = tmp_path / "repo"
    (repo / ".jj").mkdir(parents=True)
    monkeypatch.setattr(
        "jj_review.jj.client.JjClient.read_jj_review_config_list_output",
        lambda self: jj_review_config_stdout,
    )
    return repo
