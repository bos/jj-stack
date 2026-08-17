from __future__ import annotations

from pathlib import Path

import pytest

from jj_stack.cli import main
from jj_stack.errors import EXIT_PROBE
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.state.store import TrackingStore, resolve_state_path

CHANGE_ID = "abcdefghijklmno"


def _jj_workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".jj" / "repo").mkdir(parents=True)
    return repo


def test_in_use_tracks_presence_of_valid_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _jj_workspace(tmp_path)
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert main(["--repository", str(repo), "in-use"]) == 1
    assert not state_home.exists()

    store = TrackingStore.for_repo(repo)
    store.create_pr(
        CHANGE_ID,
        identity=PRIdentity(
            repo_owner="octocat",
            repo_name="example",
            pr_number=17,
            head_owner="octocat",
            head_ref="jj-stack/change-abcdefgh",
        ),
        baseline=SubmittedBaseline(commit_id="abc123"),
    )
    store.retire_pr(CHANGE_ID)
    assert store.load() == TrackingState()

    assert main(["--repository", str(repo), "in-use"]) == 0


def test_in_use_distinguishes_non_jj_directory_from_false(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--repository", str(tmp_path), "in-use"])
    captured = capsys.readouterr()

    assert exit_code == EXIT_PROBE
    assert "Not inside a jj workspace" in captured.err


def test_in_use_distinguishes_invalid_tracking_from_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _jj_workspace(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    state_path = resolve_state_path(repo)
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"version": 2}\n', encoding="utf-8")

    exit_code = main(["--repository", str(repo), "in-use"])
    captured = capsys.readouterr()

    assert exit_code == EXIT_PROBE
    assert "unsupported version 2" in captured.err
