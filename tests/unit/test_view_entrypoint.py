import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import jj_stack.commands.view as view_module
import jj_stack.console as console_module
from jj_stack.errors import EXIT_INCOMPLETE
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.review_state import ReviewState

from .entrypoint_test_helpers import patch_bootstrap


def test_view_fetches_once_and_skips_duplicate_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, view_module, tmp_path)
    fetched: list[str] = []
    rendered: list[str] = []

    monkeypatch.setattr(
        view_module,
        "refresh_remote_state_for_status",
        lambda **kwargs: fetched.append("fetched"),
    )

    def fake_prepare_status_for_revset(**kwargs):
        revset = kwargs["revset"]
        change_ids = ("change-1", "change-2") if revset in {"foo", "bar"} else ("change-3",)
        return SimpleNamespace(
            selected_revset=revset,
            prepared=SimpleNamespace(
                stack=SimpleNamespace(
                    base_parent=SimpleNamespace(
                        commit_id="shared-base" if revset in {"foo", "bar"} else f"base-{revset}"
                    ),
                    head=SimpleNamespace(change_id=change_ids[-1]),
                ),
                state=ReviewState(),
                status_revisions=tuple(
                    SimpleNamespace(revision=SimpleNamespace(change_id=change_id))
                    for change_id in change_ids
                ),
            ),
        )

    def fake_render_prepared_status(**kwargs) -> int:
        prepared_status = kwargs["prepared_status"]
        rendered.append(prepared_status.selected_revset)
        return 0

    monkeypatch.setattr(
        view_module,
        "_prepare_status_for_revset",
        fake_prepare_status_for_revset,
    )
    monkeypatch.setattr(view_module, "_render_prepared_status", fake_render_prepared_status)

    exit_code = view_module.view(
        as_json=False,
        cli_args=JjCliArgs(),
        debug=False,
        fetch=True,
        pull_request=None,
        repository=tmp_path,
        revset=None,
        selectors=(
            view_module.ViewSelector(kind="revset", value="foo"),
            view_module.ViewSelector(kind="revset", value="bar"),
            view_module.ViewSelector(kind="revset", value="baz"),
        ),
        verbose=False,
    )

    assert exit_code == 0
    assert fetched == ["fetched"]
    assert rendered == ["foo", "baz"]


def test_view_continues_after_selector_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, view_module, tmp_path)

    def fake_prepare_status_for_revset(**kwargs):
        revset = kwargs["revset"]
        if revset == "bad":
            raise view_module.CliError("bad selector")
        return SimpleNamespace(
            selected_revset=revset,
            prepared=SimpleNamespace(
                stack=SimpleNamespace(
                    base_parent=SimpleNamespace(commit_id=f"base-{revset}"),
                    head=SimpleNamespace(change_id=f"{revset}-head"),
                ),
                state=ReviewState(),
                status_revisions=(
                    SimpleNamespace(revision=SimpleNamespace(change_id=f"{revset}-change")),
                ),
            ),
        )

    def fake_render_prepared_status(**kwargs) -> int:
        console_module.output(f"rendered {kwargs['prepared_status'].selected_revset}")
        return 0

    monkeypatch.setattr(
        view_module,
        "_prepare_status_for_revset",
        fake_prepare_status_for_revset,
    )
    monkeypatch.setattr(view_module, "_render_prepared_status", fake_render_prepared_status)

    stdout = StringIO()
    stderr = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=stderr, color_mode="never"):
        exit_code = view_module.view(
            as_json=False,
            cli_args=JjCliArgs(),
            debug=False,
            fetch=False,
            pull_request=None,
            repository=tmp_path,
            revset=None,
            selectors=(
                view_module.ViewSelector(kind="revset", value="good"),
                view_module.ViewSelector(kind="revset", value="bad"),
                view_module.ViewSelector(kind="revset", value="later"),
            ),
            verbose=False,
        )

    assert exit_code == EXIT_INCOMPLETE
    stdout_lines = stdout.getvalue().splitlines()
    assert "Status for good:" in stdout_lines
    assert "rendered good" in stdout_lines
    assert "Status for bad:" in stdout_lines
    assert "Status for later:" in stdout_lines
    assert "rendered later" in stdout_lines
    assert stdout_lines.index("Status for good:") < stdout_lines.index("rendered good")
    assert stdout_lines.index("Status for bad:") < stdout_lines.index("Status for later:")
    assert stdout_lines.index("Status for later:") < stdout_lines.index("rendered later")
    assert "Error: bad selector" in stderr.getvalue()


def test_view_json_continues_after_selector_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, view_module, tmp_path)

    def fake_prepare_status_for_revset(**kwargs):
        revset = kwargs["revset"]
        if revset == "bad":
            raise view_module.CliError("bad selector")
        return SimpleNamespace(
            selected_revset=revset,
            prepared=SimpleNamespace(
                stack=SimpleNamespace(
                    base_parent=SimpleNamespace(commit_id=f"base-{revset}"),
                    head=SimpleNamespace(change_id=f"{revset}-head"),
                ),
                state=ReviewState(),
                status_revisions=(
                    SimpleNamespace(revision=SimpleNamespace(change_id=f"{revset}-change")),
                ),
            ),
        )

    def fake_json_prepared_status(**kwargs):
        selector = kwargs["selector"]
        return {"selector": selector.value, "changes": []}, False

    monkeypatch.setattr(
        view_module,
        "_prepare_status_for_revset",
        fake_prepare_status_for_revset,
    )
    monkeypatch.setattr(view_module, "_json_prepared_status", fake_json_prepared_status)

    stdout = StringIO()
    stderr = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=stderr, color_mode="never"):
        exit_code = view_module.view(
            as_json=True,
            cli_args=JjCliArgs(),
            debug=False,
            fetch=False,
            pull_request=None,
            repository=tmp_path,
            revset=None,
            selectors=(
                view_module.ViewSelector(kind="revset", value="good"),
                view_module.ViewSelector(kind="revset", value="bad"),
                view_module.ViewSelector(kind="revset", value="later"),
            ),
            verbose=False,
        )

    assert exit_code == EXIT_INCOMPLETE
    assert json.loads(stdout.getvalue()) == {
        "stacks": [
            {"selector": "good", "changes": []},
            {"selector": "later", "changes": []},
        ]
    }
    assert "Error: bad selector" in stderr.getvalue()
