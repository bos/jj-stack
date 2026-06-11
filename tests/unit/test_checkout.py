from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from jj_review.bootstrap import CommandContext
from jj_review.commands.checkout import _run_checkout_async
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.jj.client import JjClient


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

    monkeypatch.setattr("jj_review.commands.checkout._resolve_selection", fake_resolve_selection)
    monkeypatch.setattr(
        "jj_review.commands.checkout.prepare_status",
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
        "jj_review.commands.checkout.stream_status_async",
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
