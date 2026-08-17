from __future__ import annotations

import pytest

from jj_stack.commands.unstack import unstack
from jj_stack.errors import UsageError
from jj_stack.jj.cli_args import JjCliArgs


def test_unstack_rejects_remote_stack_number_with_local_only_mode_before_bootstrap(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "jj_stack.commands.unstack.bootstrap_context",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid options must not bootstrap the repo")
        ),
    )

    with pytest.raises(UsageError, match="--stack cannot be combined with --local"):
        unstack(
            cli_args=JjCliArgs(),
            debug=False,
            dry_run=False,
            local=True,
            pr=None,
            repo=None,
            revset=None,
            stack=7,
        )
