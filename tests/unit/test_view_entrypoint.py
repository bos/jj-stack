from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import jj_stack.commands.view as view_module
import jj_stack.console as console_module
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.review_state import CachedChange, ReviewState

from .entrypoint_test_helpers import patch_bootstrap


def test_view_updates_tty_progress_bar_while_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, view_module, tmp_path)
    progress_updates: list[int] = []
    progress_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        view_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            github_inspection_count=lambda: 2,
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda: {},
                    render_revision_log_lines=lambda revision, *, color_when: (
                        f"{revision.subject} [{revision.change_id[:8]}]",
                    ),
                    render_revision_log_blocks=lambda revisions, *, color_when: {
                        revision.commit_id: (
                            f"{revision.subject} [{revision.change_id[:8]}]",
                        )
                        for revision in revisions
                    },
                    resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
                ),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                state=ReviewState(),
                stack=SimpleNamespace(
                    base_parent=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    ),
                    head=SimpleNamespace(change_id="head-change-id"),
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(
                    SimpleNamespace(cached_change=CachedChange(pr_number=1)),
                    SimpleNamespace(cached_change=CachedChange(pr_number=2)),
                ),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            selected_revset="@",
            base_parent_subject="base",
        ),
    )

    @contextmanager
    def fake_progress(*, description: str, total: int):
        progress_calls.append({"description": description, "total": total})

        class Handle:
            def advance(self, amount: int = 1) -> None:
                progress_updates.append(amount)

        yield Handle()

    def fake_stream_status(**kwargs):
        assert kwargs.get("inspect_stack_comments", False) is False
        kwargs["on_revision"](object(), True)
        kwargs["on_revision"](object(), True)
        return SimpleNamespace(
            cache_update_skipped=False,
            github_error=None,
            github_repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
            incomplete=False,
            revisions=(),
            submitted_state_disagreements=(),
        )

    monkeypatch.setattr(view_module, "stream_status", fake_stream_status)
    monkeypatch.setattr(view_module.console, "progress", fake_progress)

    exit_code = view_module.view(
        cli_args=JjCliArgs(),
        debug=False,
        fetch=False,
        pull_request=None,
        repository=tmp_path,
        revset=None,
        verbose=False,
    )

    assert exit_code == 0
    assert progress_updates == [1, 1]
    assert progress_calls == [{"description": "Inspecting GitHub", "total": 2}]


def test_view_passes_cli_color_override_to_native_jj_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, view_module, tmp_path)
    observed: dict[str, object] = {}
    monkeypatch.setattr("jj_stack.formatting.requested_color_mode", lambda: "debug")
    monkeypatch.setattr(
        view_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            github_inspection_count=lambda: 0,
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda: {},
                    render_revision_log_lines=lambda revision, *, color_when: (
                        f"{revision.subject} [{revision.change_id[:8]}]",
                    ),
                    resolve_color_when=lambda *, cli_color, stdout_is_tty: observed.update(
                        cli_color=cli_color,
                        stdout_is_tty=stdout_is_tty,
                    )
                    or "never",
                ),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                state=ReviewState(),
                stack=SimpleNamespace(
                    base_parent=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    ),
                    head=SimpleNamespace(change_id="head-change-id"),
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            selected_revset="@",
            base_parent_subject="base",
        ),
    )
    monkeypatch.setattr(
        view_module,
        "stream_status",
        lambda **kwargs: SimpleNamespace(
            cache_update_skipped=False,
            github_error=None,
            github_repository=GithubRepoAddress(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
            incomplete=False,
            revisions=(),
            submitted_state_disagreements=(),
        ),
    )

    exit_code = view_module.view(
        cli_args=JjCliArgs(),
        debug=False,
        fetch=False,
        pull_request=None,
        repository=tmp_path,
        revset=None,
        verbose=False,
    )

    assert exit_code == 0
    assert observed["cli_color"] == "debug"


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

    assert exit_code == 1
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
