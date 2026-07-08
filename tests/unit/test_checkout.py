from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.checkout import _pick_tracked_stack_head, _run_checkout_async
from jj_stack.config import RepoConfig
from jj_stack.errors import CliError, UsageError
from jj_stack.jj.client import JjClient


def test_run_checkout_current_rejects_before_github_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def fake_resolve_selection(**kwargs):
        return SimpleNamespace(
            default_current_stack=True,
            selector="default current stack (@-)",
            head_bookmark=None,
            selected_revset="@-",
        )

    monkeypatch.setattr("jj_stack.commands.checkout._resolve_selection", fake_resolve_selection)
    monkeypatch.setattr(
        "jj_stack.commands.checkout.prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda bookmarks: {}),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(revisions=()),
                state_store=SimpleNamespace(load=lambda: SimpleNamespace(changes={})),
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/feature-aaaa",
                        revision=SimpleNamespace(commit_id="feature-commit"),
                    ),
                ),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            selected_revset="@",
        ),
    )

    async def fail_stream_status_async(**kwargs):
        raise AssertionError("GitHub inspection should not run for this failure path.")

    monkeypatch.setattr(
        "jj_stack.commands.checkout.stream_status_async",
        fail_stream_status_async,
    )

    jj_client = SimpleNamespace(repo_root=tmp_path, query_revisions=lambda *args, **kwargs: ())
    context = cast(
        CommandContext,
        SimpleNamespace(
            config=RepoConfig(),
            jj_client=cast(JjClient, jj_client),
            state_store=SimpleNamespace(),
        ),
    )

    with pytest.raises(CliError) as exc_info:
        asyncio.run(
            _run_checkout_async(
                context=context,
                fetch=False,
                pull_request_reference=None,
                revset=None,
            )
        )

    assert "has no matching remote pull request" in str(exc_info.value)


def test_pick_tracked_stack_head_reports_missing_or_invalid_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = cast(
        CommandContext,
        SimpleNamespace(
            jj_client=SimpleNamespace(),
            state_store=SimpleNamespace(load=lambda: SimpleNamespace(changes={})),
        ),
    )

    monkeypatch.setattr(
        "jj_stack.commands.checkout.discover_tracked_stacks",
        lambda **kwargs: SimpleNamespace(current_commit_id=None, stacks=()),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    with pytest.raises(CliError, match="No locally tracked stacks"):
        _pick_tracked_stack_head(context)

    stack = SimpleNamespace(
        head=SimpleNamespace(change_id="change-1", subject="feature 1"),
        revisions=(SimpleNamespace(commit_id="commit-1"),),
    )
    monkeypatch.setattr(
        "jj_stack.commands.checkout.discover_tracked_stacks",
        lambda **kwargs: SimpleNamespace(current_commit_id="commit-1", stacks=(stack,)),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("9\n"))
    with pytest.raises(UsageError, match="not a valid stack number"):
        _pick_tracked_stack_head(context)
