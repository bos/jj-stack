from __future__ import annotations

import io
from types import SimpleNamespace
from typing import cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.checkout import _pick_tracked_stack_head
from jj_stack.errors import CliError, UsageError


def test_pick_tracked_stack_head_reports_missing_or_invalid_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = cast(
        CommandContext,
        SimpleNamespace(
            jj_client=SimpleNamespace(),
            state_store=SimpleNamespace(
                load=lambda: SimpleNamespace(review_identities={"change-1": object()})
            ),
        ),
    )

    monkeypatch.setattr(
        "jj_stack.commands.checkout.observe_repository_paths",
        lambda **kwargs: SimpleNamespace(paths=()),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    with pytest.raises(CliError, match="No locally tracked stacks"):
        _pick_tracked_stack_head(context)

    stack = SimpleNamespace(
        head=SimpleNamespace(change_id="change-1", subject="feature 1"),
        revisions=(SimpleNamespace(commit_id="commit-1"),),
    )
    monkeypatch.setattr(
        "jj_stack.commands.checkout.observe_repository_paths",
        lambda **kwargs: SimpleNamespace(
            paths=(SimpleNamespace(stack=stack, tracked_change_ids={"change-1"}),)
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("9\n"))
    with pytest.raises(UsageError, match="not a valid stack number"):
        _pick_tracked_stack_head(context)
